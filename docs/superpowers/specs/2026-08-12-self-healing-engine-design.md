# SCM-OVS Self-Healing Engine Design

**Date:** 2026-08-12
**Owner:** Codex / GPT (`scm_agent/healing/**`)
**Status:** Approved for implementation by the project owner’s instruction to let the agent council decide and build the complete healing lane for later review.

## 1. Goal and boundaries

Build the PostgreSQL-only, rule-based healing pillar without changing a monitored database’s schema. The engine will collect health statistics, detect five anomaly classes, write an external append-only audit trail, and apply only actions that have defensible safety gates.

The engine does not implement ML anomaly detection, other database engines, a dashboard, or the NL-to-SQL pipeline. It does not edit files owned by Claude under `scm_agent/pipeline/**`, `scm_agent/eval/**`, or `scm_agent/db/**`.

PostgreSQL 12+ is the supported protocol floor because safe index rebuilding requires `REINDEX ... CONCURRENTLY`; the project’s container is PostgreSQL 16 on host port 5434. Core tests must run on Python 3.12 using only the standard library. The `psycopg` import stays lazy so a missing driver blocks only live PostgreSQL commands, not pure rules, SQLite audit tests, or orchestration tests.

## 2. Decisions from the architecture council

Three independent agents reviewed monitoring/rules, corrective-action safety, and audit/orchestration. Their reconciled decisions are:

1. `n_dead_tup / (n_live_tup + n_dead_tup)` is described as a **dead-tuple pressure proxy**, not a precise physical-bloat measurement.
2. `idx_scan = 0` and size indicate a **possibly unused large index**, not index bloat. This rule never emits `REINDEX` or `DROP INDEX` automatically.
3. The engine implements safe `REINDEX INDEX CONCURRENTLY` construction and execution capability, but v1 exposes it only as an explicitly approved manual action. There is no automatic rule with strong enough evidence.
4. Automatic actions are limited to plain online `VACUUM` and, when a separate high-risk flag is enabled, exact-session termination for a repeatedly detected idle-in-transaction backend.
5. Long-running queries, saturation, and unused indexes are advisory. The engine never selects arbitrary connection victims or kills active queries.
6. Audit data lives in application-owned SQLite, never in the monitored database. The target DSN and raw query text are never persisted.
7. `observe` is the default. `dry_run` plans and revalidates but executes nothing. `act` is explicit and still obeys action allowlists, cooldowns, privileges, and revalidation.
8. A successful command is not automatically a verified heal. Immediate and later snapshots preserve that distinction.

## 3. Package design

All production and test code lives below `scm_agent/healing/`.

| Module | Responsibility |
|---|---|
| `models.py` | Frozen typed observations, findings, action plans/results, enums, fingerprints, and serialization-safe evidence. |
| `config.py` | Healing-only policy and runtime configuration from safe defaults and environment variables; validates every range and duration. |
| `rules.py` | Pure detection functions for all five anomaly types. No I/O and no SQL. |
| `audit.py` | Application-owned SQLite repository, append-only transitions, snapshots, event correlation, cooldown queries, and orphan reconciliation. |
| `actions.py` | Fixed anomaly-to-action mapping, PostgreSQL identifier quoting, canonical commands, policy gates, and action result classification. No caller-supplied SQL. |
| `postgres.py` | Lazy-`psycopg` monitoring/action adapter, fixed catalog queries, row mapping, privileges, OID/PID revalidation, autocommit execution, and advisory locks. |
| `engine.py` | One-cycle and daemon orchestration, failure isolation, consecutive-detection gates, serial action budget, and mode semantics. |
| `cli.py`, `__main__.py` | `doctor`, `once`, `run`, `events`, `actions`, and explicit manual `reindex` commands. DSNs come from environment, never CLI arguments. |
| `demo.py` | Guarded, disposable PostgreSQL demo scenarios for bloat, idle transaction, and connection pressure. |
| `tests/` | Dependency-free unit/integration-with-fakes tests plus opt-in live PostgreSQL tests. |

The core uses small protocols for a collector/action gateway, audit repository, clock, and sleeper. That keeps rule and engine behavior testable without a running database.

## 4. Observations and findings

All observations use timezone-aware UTC datetimes and stable database identities:

- `ActivitySample`: database OID/name, PID, `backend_start`, role/application, backend type, state, nullable transaction/query/state timestamps, and wait fields. Raw SQL is excluded.
- `TableStatsSample`: database OID, relation OID, schema/name, live/dead estimates, vacuum timestamps and counters, and relation sizes.
- `IndexStatsSample`: relation/index OIDs and names, size, scan counters, validity/readiness/liveness, uniqueness/primary/constraint flags, statistics reset and server-start timestamps.
- `ConnectionSample`: observed time, cluster-wide client-backend count, maximum connections, and reserved slots.
- `HealthSnapshot`: successful observation groups plus per-collector errors. A collector error makes dependent state unknown; it never silently clears an event.

A `Finding` contains a rule ID/version, severity, target type/key, human explanation, structured evidence, recommendation, autonomous-action eligibility, timestamp, and stable fingerprint. Relation fingerprints use database OID plus relation OID; backend fingerprints include database OID, PID, `backend_start`, transaction start, and state-change timestamp to prevent PID-reuse mistakes.

## 5. Detection policy and exact semantics

Defaults are deliberately conservative and configurable:

| Setting | Default |
|---|---:|
| Poll interval | 30 seconds |
| Dead-tuple ratio | 0.20 |
| Minimum estimated table rows | 10,000 |
| Minimum dead tuples | 5,000 |
| Idle-in-transaction warning | 5 minutes |
| Idle-in-transaction action eligibility | 15 minutes |
| Long-running active query | 5 minutes |
| Connection warning / critical | 0.80 / 0.90 |
| Large unused index | 100 MiB |
| Maximum scans considered unused | 0 |
| Minimum trustworthy statistics age | 7 days |
| Consecutive detections before any action | 2 |
| VACUUM success cooldown | 6 hours |
| Maximum actions per cycle | 1 |
| Idle-transaction termination | disabled |

Rules use strict threshold crossing (`>`), so equality does not fire unless a rule explicitly defines a band boundary. Invalid or future timestamps, zero denominators, negative counts, null required timestamps, system/self backends, and untrustworthy index-statistics windows do not produce destructive findings.

- **Dead-tuple pressure:** total rows, dead count, and ratio must all cross thresholds. Recommends online `VACUUM`.
- **Idle in transaction:** accepts `idle in transaction` and `idle in transaction (aborted)` for warning. Only exact `idle in transaction` may become action-eligible, after the longer threshold and the feature flag.
- **Long query:** requires `state = active` and a valid `query_start`; advisory only.
- **Saturation:** uses cluster-wide client-backend count divided by `max_connections`; advisory only.
- **Unused large index:** requires a trustworthy statistics window, size and scan thresholds, and valid/live/ready status; primary, unique, and constraint-backed indexes are excluded; advisory only.

## 6. PostgreSQL monitoring

The monitor uses fixed, catalog-qualified, read-only SQL against:

- `pg_catalog.pg_stat_activity`, scoped to the current database for session rules, excluding `pg_backend_pid()` and requiring `backend_type = 'client backend'`;
- cluster-wide `pg_stat_activity` plus `current_setting('max_connections')` for saturation;
- `pg_catalog.pg_stat_user_tables` for tuple estimates and vacuum state;
- `pg_catalog.pg_stat_user_indexes`, `pg_index`, `pg_constraint`, `pg_stat_database`, and `pg_relation_size()` for index evidence.

Monitoring connections use the application name `scm-ovs/monitor`, autocommit or short transactions, a bounded statement timeout, and no schema changes. Full cross-user activity visibility is reported as a capability requiring superuser, `pg_monitor`, or `pg_read_all_stats`.

## 7. Action safety

No API accepts raw SQL. Plans carry typed target identities and a fixed action enum.

Canonical maintenance commands are:

```sql
VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) "schema"."table"
REINDEX INDEX CONCURRENTLY "schema"."index"
```

Identifiers are separately quoted and embedded quotes are doubled. `VACUUM FULL`, plain `REINDEX`, `DROP INDEX`, arbitrary options, and arbitrary SQL are impossible through the public API.

`VACUUM` and concurrent reindex use a dedicated autocommit action connection with `application_name = scm-ovs/healer`, a low lock timeout, bounded statement timeout, OID/name/type revalidation immediately before execution, and a session advisory lock. `VACUUM` acts only on ordinary permanent user tables. Concurrent reindex additionally rejects partitioned, temporary, system, exclusion-constraint, invalid, unready, or non-live indexes.

Session termination is a single conditional PostgreSQL statement that rechecks captured PID, `backend_start`, transaction start, state-change time, database OID, backend type, exact state and age. It also rejects the healer’s own PID/application, protected roles, and superuser targets. Zero matching rows is a safe stale/precondition skip. Project policy requires superuser or active `pg_signal_backend`, even if PostgreSQL would allow signaling the same role.

Privilege checks branch by server version: PostgreSQL 12–16 uses superuser/active owner/database-owner authority; PostgreSQL 17+ also understands `MAINTAIN`/`pg_maintain`. Execution still catches privilege changes and records SQLSTATE.

Action mapping:

| Finding/action source | v1 behavior |
|---|---|
| Dead-tuple pressure | May plan/execute `VACUUM` in `dry_run`/`act` after all gates. |
| Idle in transaction | May terminate only in `act`, after two detections, with the separate high-risk flag. |
| Long query | Advisory only. |
| Connection saturation | Advisory only. |
| Unused large index | Advisory only. |
| Explicit manual index target | `dry_run` or explicitly confirmed `act` concurrent reindex after all eligibility checks. |

There is never an immediate retry after a maintenance failure. Connection-loss outcomes are classified as unknown and held through the conservative cooldown pending re-observation.

## 8. Audit model

The default file is `var/scm_ovs/healing-audit.sqlite3`. Its parent directory is created by the application. SQLite enables foreign keys, WAL, and a busy timeout.

The bounded normalized schema is:

- `monitored_target`: sanitized label and one-way DSN identity hash;
- `poll_cycle`: immutable cycle metadata and configuration digest;
- `cycle_transition`: started/completed/failed;
- `anomaly_event`: stable fingerprint and first-seen identity;
- `event_transition`: detected/still_present/planned/suppressed/cleared;
- `action_attempt`: immutable plan, mode, canonical command, parameters, reason and idempotency key;
- `action_transition`: planned/precondition_rejected/simulated/executing/succeeded/failed/outcome_unknown/verified/verification_failed;
- `snapshot`: detection/before/after/verification/failure JSON evidence.

Triggers reject update and delete operations on audit tables. This is logical append-only behavior, not a claim of tamper-proof storage against an OS user who controls the SQLite file.

Repeated findings append `still_present` to the same open event. `cleared` is appended only when the relevant collector succeeded and the rule evaluated false. Dry runs never consume cooldown. Cooldown and idempotency consult successful real actions only.

The execution saga persists plan and intent before touching PostgreSQL, closes the SQLite transaction, performs the action, then persists outcome and snapshots. On startup, orphaned `executing` attempts become `outcome_unknown`; the engine re-observes and never blindly repeats them.

No DSN, password, or raw query text is stored. Errors are length-bounded and sanitized.

## 9. Engine and operating modes

One non-overlapping cycle at a time:

1. Persist cycle start and configuration digest.
2. Collect each observation group independently.
3. Run pure dependent rules where collection succeeded.
4. Correlate findings and event transitions.
5. In `observe`, stop after audit.
6. In `dry_run`/`act`, map only eligible findings to typed plans.
7. Apply consecutive-detection, feature-flag, cooldown, privilege, action-budget, advisory-lock, and immediate-revalidation gates.
8. Persist before intent; simulate or execute serially; record result and best-effort after snapshot.
9. Persist cycle completion/failure.

An audit write failure fails closed before any action. One collector or action failure is contained and recorded rather than aborting unrelated detection. The daemon uses a monotonic wait and never overlaps a slow cycle.

## 10. CLI and demos

`python -m scm_agent.healing` provides:

- `doctor`: driver, database, server version, monitoring visibility, action privileges, and audit-path checks;
- `once [--mode observe|dry_run|act]`: one cycle with JSON summary;
- `run [--mode ...]`: repeated cycles until interrupted;
- `events` and `actions`: audit inspection;
- `reindex --schema NAME --index NAME --mode dry_run|act --confirm`: explicit manual-only concurrent reindex;
- `demo bloat|idle-tx|pressure setup|hold|cleanup`: guarded disposable scenarios.

The DSN is read from `SCM_DB_DSN`, defaulting to the disposable development database on port 5434. It is never accepted as a CLI argument or printed. `act` is never the default. Manual reindex in act mode requires both the word-level confirmation flag and an explicit environment allow flag.

Demo helpers refuse to run unless the configured database matches a disposable-name guard or `SCM_HEALING_ALLOW_DEMO=1`. They use `scm_ovs_demo_*` names and tagged application names. Connection pressure uses a configurable low threshold; it does not exhaust the server deliberately.

## 11. Testing and acceptance criteria

The default command is:

```bash
python -m unittest discover -s scm_agent/healing/tests -v
```

It must work without `psycopg` or Docker and cover:

- every threshold just below, equal, and above;
- null/future timestamps, zero denominators, counter resets, system/self exclusions;
- all action mappings and proof that advisory rules cannot emit destructive plans;
- identifier quoting and injection-like identifier strings;
- observe/dry-run/act execution matrix;
- cooldown, consecutive-detection, action budget, audit failure, stale precondition, and unknown-outcome behavior;
- append-only SQLite triggers, event correlation, snapshots, orphan recovery, and redaction;
- engine failure isolation with fake adapters;
- CLI parsing and secret-free output.

Opt-in live tests require `SCM_TEST_PG_DSN`. Destructive tests also require `SCM_TEST_PG_DISPOSABLE=1` and a disposable database-name check. They cover real catalog collection, no target schema changes in observe/dry-run, online vacuum, exact idle-session termination, autocommit requirements, advisory locking, privilege failures, and explicit concurrent reindex.

The implementation is accepted when the dependency-free suite passes, the package compiles, command help runs without the driver, no Claude-owned source file changed, the live suite is present and safely skipped when prerequisites are absent, and `PROJECT_STATE.md` accurately distinguishes verified unit behavior from unverified live PostgreSQL behavior.

## 12. Deferred beyond v1

- Automatic concurrent reindex, until a defensible bloat/corruption signal exists.
- Automatic active-query cancellation or generic connection eviction.
- Accurate physical-bloat extensions such as `pgstattuple`.
- Multi-process daemon leadership beyond local lock plus PostgreSQL action locks.
- Tamper-evident hash chaining or remote audit replication.
- Web/API/dashboard integration in `app/main.py`.
- PostgreSQL drivers, Docker startup, and live-test execution until the machine’s sudo-controlled prerequisites are installed by the user.
