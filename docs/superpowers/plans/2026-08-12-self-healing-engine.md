# SCM-OVS Self-Healing Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete PostgreSQL-only healing lane with five rule-based monitors, conservative corrective actions, an external append-only audit journal, three operating modes, CLI/demo support, and dependency-free tests.

**Architecture:** Frozen typed observations flow from a lazy Psycopg adapter into pure rules. A mode-aware engine correlates findings in an append-only SQLite repository, applies durable gates, and sends typed plans to a revalidating PostgreSQL action adapter. The core depends on protocols and the standard library so tests run without PostgreSQL or Psycopg.

**Tech Stack:** Python 3.12 standard library (`dataclasses`, `enum`, `datetime`, `sqlite3`, `argparse`, `unittest`, `typing`); optional Psycopg 3 for live PostgreSQL 12+; PostgreSQL 16 development container on host port 5434.

## Global Constraints

- Production and test code changes stay under `scm_agent/healing/**` except the already-approved design/plan documents and an announced final update to `PROJECT_STATE.md`.
- Do not edit `scm_agent/pipeline/**`, `scm_agent/eval/**`, `scm_agent/db/**`, `docker-compose.yml`, `requirements.txt`, `app/main.py`, `AGENTS.md`, or `CLAUDE.md`.
- Default mode is exactly `observe`; `act` is never implicit.
- Never emit `VACUUM FULL`, plain `REINDEX`, `DROP INDEX`, arbitrary SQL, or arbitrary connection termination.
- Only dead-tuple pressure can plan automatic online `VACUUM`; only exact idle-in-transaction findings can plan termination, and termination requires a separate disabled-by-default flag.
- Long-running queries, saturation, and unused indexes are advisory and cannot produce destructive automatic plans.
- Concurrent reindex is explicit/manual only and requires a separate disabled-by-default flag plus CLI confirmation.
- The monitored database receives no schema objects. Audit data is SQLite outside the target database.
- Never persist or print the target DSN/password or raw query text.
- Core imports and the default test suite work without `psycopg`; import it only inside live-adapter entry points.
- Use strict TDD: add a focused failing test, run it and confirm the expected failure, implement minimally, then rerun the focused and full dependency-free suites.
- Use `python -m unittest discover -s scm_agent/healing/tests -v` as the dependency-free full-suite command.
- Preserve unrelated concurrent changes from Claude and stage commits by explicit path.

---

### Task 1: Typed domain, validated configuration, and pure rules

**Files:**
- Create: `scm_agent/healing/__init__.py`
- Create: `scm_agent/healing/models.py`
- Create: `scm_agent/healing/config.py`
- Create: `scm_agent/healing/rules.py`
- Create: `scm_agent/healing/tests/__init__.py`
- Create: `scm_agent/healing/tests/test_config.py`
- Create: `scm_agent/healing/tests/test_rules.py`

**Interfaces:**
- Produce enums `Mode`, `Severity`, `RuleId`, `TargetKind`, `ActionKind`, and `ActionStatus`.
- Produce frozen dataclasses `ActivitySample`, `TableStatsSample`, `IndexStatsSample`, `ConnectionSample`, `CollectionError`, `HealthSnapshot`, `Finding`, `ActionPlan`, and `ActionResult`.
- Produce frozen `RulePolicy` and `HealingConfig`, with `HealingConfig.from_env(environ: Mapping[str, str] | None = None) -> HealingConfig`.
- Produce `detect_all(snapshot: HealthSnapshot, policy: RulePolicy) -> tuple[Finding, ...]` and one public pure function per rule.
- Later tasks consume findings with stable `fingerprint`, `target_key`, JSON-safe `evidence`, `recommendation`, and `action_eligible` fields.

- [ ] **Step 1: Write failing configuration tests**

Cover safe defaults and validation using literal expected values. Representative API:

```python
def test_from_env_defaults_to_observe_and_safe_actions_off(self):
    config = HealingConfig.from_env({})
    self.assertIs(config.mode, Mode.OBSERVE)
    self.assertFalse(config.allow_terminate_idle_transaction)
    self.assertFalse(config.allow_manual_reindex)
    self.assertEqual(config.database_dsn, "postgresql://scm:scm@localhost:5434/scm_demo")

def test_policy_rejects_overlapping_connection_bands(self):
    with self.assertRaisesRegex(ValueError, "connection"):
        RulePolicy(connection_warning_ratio=0.95, connection_critical_ratio=0.90)
```

- [ ] **Step 2: Run configuration tests and verify RED**

Run:

```bash
python -m unittest scm_agent.healing.tests.test_config -v
```

Expected: import failure because `scm_agent.healing.config` does not exist.

- [ ] **Step 3: Implement domain types and configuration**

Use frozen dataclasses and timezone-aware validation. Exact policy defaults:

```python
@dataclass(frozen=True)
class RulePolicy:
    dead_tuple_ratio: float = 0.20
    minimum_table_rows: int = 10_000
    minimum_dead_tuples: int = 5_000
    idle_transaction_warning: timedelta = timedelta(minutes=5)
    idle_transaction_action: timedelta = timedelta(minutes=15)
    long_query: timedelta = timedelta(minutes=5)
    connection_warning_ratio: float = 0.80
    connection_critical_ratio: float = 0.90
    unused_index_minimum_bytes: int = 100 * 1024 * 1024
    unused_index_maximum_scans: int = 0
    minimum_statistics_age: timedelta = timedelta(days=7)
```

`HealingConfig` must also validate poll interval, audit path, action budget, required consecutive detections, six-hour vacuum cooldown, timeouts, protected roles, and feature flags. Parse booleans from a strict set (`1/true/yes/on`, `0/false/no/off`) and reject other values. `safe_summary()` must omit the DSN.

- [ ] **Step 4: Run configuration tests and verify GREEN**

Run the focused configuration tests, then:

```bash
python -m unittest discover -s scm_agent/healing/tests -v
```

Expected: all current tests pass.

- [ ] **Step 5: Write failing pure-rule tests**

Use hand-built UTC fixtures and literal expected rule IDs/severities. Cover below/equal/above boundaries, zero denominators, null/future timestamps, both idle transaction states, active-only long queries, system/self exclusions, warning/critical connection bands, statistics age/reset, and primary/unique/constraint index exclusions.

Representative assertions:

```python
self.assertEqual(detect_table_pressure((sample_at_ratio_020,), policy), ())
self.assertEqual(detect_table_pressure((sample_above_ratio,), policy)[0].rule_id, RuleId.DEAD_TUPLE_PRESSURE)
self.assertFalse(detect_long_queries((active_old,), policy)[0].action_eligible)
self.assertEqual(detect_unused_indexes((primary_large_zero_scan,), policy), ())
```

- [ ] **Step 6: Run rule tests and verify RED**

Run:

```bash
python -m unittest scm_agent.healing.tests.test_rules -v
```

Expected: failures because rule functions are missing.

- [ ] **Step 7: Implement the five pure rules**

Implement:

```python
def detect_table_pressure(samples: Iterable[TableStatsSample], policy: RulePolicy) -> tuple[Finding, ...]: ...
def detect_idle_transactions(samples: Iterable[ActivitySample], policy: RulePolicy) -> tuple[Finding, ...]: ...
def detect_long_queries(samples: Iterable[ActivitySample], policy: RulePolicy) -> tuple[Finding, ...]: ...
def detect_connection_saturation(sample: ConnectionSample | None, policy: RulePolicy) -> tuple[Finding, ...]: ...
def detect_unused_indexes(samples: Iterable[IndexStatsSample], policy: RulePolicy) -> tuple[Finding, ...]: ...
```

Ratios and ages use the observation’s `observed_at`, never wall-clock access inside a rule. Sort results deterministically by rule ID and target key. Do not include query text in evidence.

- [ ] **Step 8: Run focused and full tests, then commit**

Run both test modules and the full discovery command. Stage only Task 1 paths and commit:

```bash
git add scm_agent/healing
git commit -m "feat(healing): add typed anomaly detection rules"
```

---

### Task 2: Append-only SQLite audit repository

**Files:**
- Create: `scm_agent/healing/audit.py`
- Create: `scm_agent/healing/tests/test_audit.py`

**Interfaces:**
- Consume `Mode`, `Finding`, `ActionPlan`, `ActionResult` and JSON-safe model serialization.
- Produce `SQLiteAuditRepository(path: Path, *, clock: Clock | None = None)`.
- Produce methods for target registration, cycle transitions, event correlation, finding transitions, action attempts/transitions, snapshots, cooldown lookup, consecutive-detection count, listing events/actions, and orphan reconciliation.
- Later engine code must be able to persist intent before execution and query successful-action cooldowns atomically.

- [ ] **Step 1: Write failing schema and append-only tests**

Use `tempfile.TemporaryDirectory`. Assert foreign keys are enabled, the expected seven logical tables exist, and direct update/delete attempts raise `sqlite3.IntegrityError`. Verify target registration stores a sanitized label and hash but not a DSN substring.

- [ ] **Step 2: Run audit tests and verify RED**

```bash
python -m unittest scm_agent.healing.tests.test_audit -v
```

Expected: import failure because `audit.py` does not exist.

- [ ] **Step 3: Implement schema initialization**

Create `monitored_target`, `poll_cycle`, `cycle_transition`, `anomaly_event`, `event_transition`, `action_attempt`, `action_transition`, and `snapshot`. Enable `PRAGMA foreign_keys=ON`, WAL, and `busy_timeout=5000`. Add per-table triggers that abort `UPDATE` and `DELETE`. Derive current state using ordered transition queries rather than mutable status columns.

- [ ] **Step 4: Add failing lifecycle tests**

Cover first detection versus `still_present`, dependent-collector-safe clearing, consecutive counts, dry runs not consuming cooldown, successful real actions consuming cooldown, unique idempotency keys, before/after snapshots, and deterministic list output.

- [ ] **Step 5: Implement repository lifecycle operations**

Use short explicit SQLite transactions and parameterized SQL. Public write operations return generated IDs. `record_action_intent()` must commit before returning. Error strings are sanitized and length-bounded. Serialize JSON with sorted keys. Do not hold a transaction open while external work runs.

- [ ] **Step 6: Add failing orphan-recovery and concurrent-lease tests**

Create an `executing` attempt without a terminal transition, reopen the repository, call recovery, and assert `outcome_unknown` is appended. Start two repository instances and assert exactly one can acquire the same action lease/idempotency key.

- [ ] **Step 7: Implement recovery and atomic lease behavior**

Use a uniqueness constraint and `BEGIN IMMEDIATE` where needed. Recovery never retries an action; it appends an outcome transition. An unavailable audit repository must raise before callers execute PostgreSQL.

- [ ] **Step 8: Run focused/full tests and commit**

```bash
python -m unittest scm_agent.healing.tests.test_audit -v
python -m unittest discover -s scm_agent/healing/tests -v
git add scm_agent/healing/audit.py scm_agent/healing/tests/test_audit.py
git commit -m "feat(healing): add append-only SQLite audit journal"
```

---

### Task 3: Conservative action planning and command safety

**Files:**
- Create: `scm_agent/healing/actions.py`
- Create: `scm_agent/healing/tests/test_actions.py`

**Interfaces:**
- Consume `Finding`, `HealingConfig`, `ActionKind`, `ActionPlan`, and target identity dataclasses.
- Produce `quote_identifier(value: str) -> str`, `qualified_identifier(schema: str, name: str) -> str`, `build_vacuum_command(schema: str, table: str) -> str`, and `build_reindex_command(schema: str, index: str) -> str`.
- Produce `ActionPlanner.plan(finding, config) -> ActionPlan | None` and `ActionPlanner.plan_manual_reindex(...) -> ActionPlan`.
- Plans contain fixed action kind, typed target, canonical SQL/template and parameters, but never arbitrary user SQL.

- [ ] **Step 1: Write failing identifier and command tests**

Use a literal corpus: keywords, dots, spaces, mixed case, embedded double quotes, Unicode, semicolons, and comment-looking strings. Exact expected example:

```python
self.assertEqual(
    build_vacuum_command('Odd"Schema', "select"),
    'VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) "Odd""Schema"."select"',
)
```

Assert empty/NUL-containing identifiers are rejected and no public planner accepts a raw SQL argument.

- [ ] **Step 2: Run action tests and verify RED**

```bash
python -m unittest scm_agent.healing.tests.test_actions -v
```

Expected: missing module/functions.

- [ ] **Step 3: Implement fixed command construction**

Double embedded PostgreSQL identifier quotes and quote each schema/object component separately. Implement only the two canonical maintenance commands. Termination uses a fixed parameterized SQL template stored in the plan, not interpolated values.

- [ ] **Step 4: Write failing mapping and mode tests**

Prove table pressure maps to vacuum; idle transaction maps only when action-eligible and feature-enabled; long query, saturation and unused index return `None`; manual reindex rejects missing feature flag, missing confirmation, system schema, or `observe` mode. Prove `observe` creates no executable plan and `dry_run`/`act` retain their mode.

- [ ] **Step 5: Implement `ActionPlanner`**

Keep policy decisions as machine-readable reason codes. Termination remains disabled by default. Manual reindex must require `allow_manual_reindex`, mode `DRY_RUN` or `ACT`, and `confirmed=True` for act. Planner logic alone does not claim privileges or freshness; the PostgreSQL gateway owns those live checks.

- [ ] **Step 6: Run focused/full tests and commit**

```bash
python -m unittest scm_agent.healing.tests.test_actions -v
python -m unittest discover -s scm_agent/healing/tests -v
git add scm_agent/healing/actions.py scm_agent/healing/tests/test_actions.py
git commit -m "feat(healing): add conservative action planning"
```

---

### Task 4: Lazy Psycopg monitoring and action adapter

**Files:**
- Create: `scm_agent/healing/postgres.py`
- Create: `scm_agent/healing/tests/test_postgres_adapter.py`
- Create: `scm_agent/healing/tests/test_postgres_live.py`

**Interfaces:**
- Produce `PsycopgUnavailableError`, `PostgresCapability`, and `PostgresGateway`.
- `PostgresGateway.collect_health() -> HealthSnapshot` executes fixed collector queries independently.
- `check_plan(plan) -> CapabilityDecision`, `capture_before(plan) -> Mapping[str, object]`, `execute(plan) -> ActionResult`, and `capture_after(plan) -> Mapping[str, object]` implement live gates.
- `doctor() -> Mapping[str, object]` returns a secret-free capability report.

- [ ] **Step 1: Write failing lazy-import and SQL-contract tests**

Import `scm_agent.healing.postgres` in the driverless environment and assert import succeeds. Calling the live connection entry point must raise a clear `PsycopgUnavailableError` with the safe installation instruction `pip install "psycopg[binary]"`. Assert fixed collector SQL is catalog-qualified and contains self/backend filters; assert the saturation query is not scoped to the current database.

- [ ] **Step 2: Run adapter tests and verify RED**

```bash
python -m unittest scm_agent.healing.tests.test_postgres_adapter -v
```

Expected: missing module.

- [ ] **Step 3: Implement lazy loading, queries, and row mapping**

Keep `import psycopg` inside `_load_psycopg()`. Use fixed SQL constants for activity, table, index, saturation, server facts, visibility and privilege checks. Monitoring uses `application_name=scm-ovs/monitor`, bounded timeouts, and no writes. Map full rows into typed dataclasses; malformed/null-required fields become `CollectionError` rather than crashes.

- [ ] **Step 4: Write failing live-gate tests using a fake DB-API adapter**

The fake must exercise observable outcomes, not mock call counts: stale OID/name/type, system/temporary relation, version below 12, missing ownership/MAINTAIN, own PID, PID reuse, changed state timestamps, protected/superuser target, and advisory-lock failure all return non-executing results with exact status/reason.

- [ ] **Step 5: Implement privilege, freshness, execution and verification gates**

Use a dedicated `autocommit=True` action connection with `application_name=scm-ovs/healer`. Set lock/statement timeouts with bound values. Resolve relation fingerprints immediately before maintenance. Acquire/release session advisory locks. For termination, use one conditional `SELECT pg_terminate_backend(pid)` statement with the full backend fingerprint predicates. Classify at least privilege (`42501`), lock timeout (`55P03`), statement timeout/cancel (`57014`), stale target, false termination result, and connection-loss unknown outcome.

- [ ] **Step 6: Add opt-in live tests**

Decorate live tests to skip unless `SCM_TEST_PG_DSN` is present. Destructive tests additionally require `SCM_TEST_PG_DISPOSABLE=1` and a database name matching the configured disposable guard. Cover collectors, observe-only schema invariance, online vacuum/autocommit, exact idle-session termination, manual concurrent reindex, and cleanup in `finally`.

- [ ] **Step 7: Run focused/full tests and commit**

The live module must skip cleanly on this machine:

```bash
python -m unittest scm_agent.healing.tests.test_postgres_adapter -v
python -m unittest scm_agent.healing.tests.test_postgres_live -v
python -m unittest discover -s scm_agent/healing/tests -v
git add scm_agent/healing/postgres.py scm_agent/healing/tests/test_postgres_adapter.py scm_agent/healing/tests/test_postgres_live.py
git commit -m "feat(healing): add PostgreSQL monitoring and safe executor"
```

---

### Task 5: Mode-aware engine orchestration

**Files:**
- Create: `scm_agent/healing/engine.py`
- Create: `scm_agent/healing/tests/test_engine.py`

**Interfaces:**
- Produce small protocols `HealthGateway`, `AuditRepository`, `Clock`, and `Sleeper`.
- Produce `HealingEngine.run_once() -> CycleSummary` and `HealingEngine.run_forever(stop: Callable[[], bool]) -> None`.
- Consume Task 1 rules/config, Task 2 audit lifecycle, Task 3 planner, and Task 4 gateway.

- [ ] **Step 1: Write failing mode-matrix tests with real SQLite and fake gateway**

Assert `observe` persists findings and makes no plans/executions; `dry_run` captures/revalidates and records `simulated` but executes nothing; `act` executes only eligible plans. Assert audit failure before intent results in zero external actions.

- [ ] **Step 2: Run engine tests and verify RED**

```bash
python -m unittest scm_agent.healing.tests.test_engine -v
```

Expected: missing engine module.

- [ ] **Step 3: Implement one-cycle orchestration**

Follow the exact saga:

```text
cycle start -> collect -> detect -> correlate -> plan -> durable gates
-> live capability/revalidation -> persist intent -> simulate OR execute
-> after snapshot -> terminal transition -> cycle terminal transition
```

Apply two consecutive detections, six-hour successful-vacuum cooldown, once-per-backend termination identity, maximum one action per cycle, and serial order. Dry runs do not consume cooldown. Collector errors suppress dependent rules without clearing their events.

- [ ] **Step 4: Add failing resilience tests**

Cover one collector group failing, one action failing while cycle completion remains recorded, stale precondition skip, unknown result, orphan reconciliation on startup, max action budget, deterministic action priority, and no automatic destructive plans from advisory findings.

- [ ] **Step 5: Implement failure isolation and daemon loop**

Use monotonic waiting and do not overlap cycles. `run_forever` responds to a supplied stop predicate and never logs DSNs. Cycle summaries contain counts/status only. Recover orphaned actions before the first cycle.

- [ ] **Step 6: Run focused/full tests and commit**

```bash
python -m unittest scm_agent.healing.tests.test_engine -v
python -m unittest discover -s scm_agent/healing/tests -v
git add scm_agent/healing/engine.py scm_agent/healing/tests/test_engine.py
git commit -m "feat(healing): orchestrate audited healing cycles"
```

---

### Task 6: CLI, guarded demo helpers, and operator documentation

**Files:**
- Create: `scm_agent/healing/cli.py`
- Create: `scm_agent/healing/__main__.py`
- Create: `scm_agent/healing/demo.py`
- Create: `scm_agent/healing/README.md`
- Create: `scm_agent/healing/requirements-live.txt`
- Create: `scm_agent/healing/tests/test_cli.py`
- Create: `scm_agent/healing/tests/test_demo.py`

**Interfaces:**
- `python -m scm_agent.healing --help` must work without Psycopg.
- Commands: `doctor`, `once`, `run`, `events`, `actions`, `reindex`, and `demo`.
- DSN comes only from environment/config, never a command argument.

- [ ] **Step 1: Write failing CLI parsing and secret-safety tests**

Capture stdout/stderr for help, observe-mode once with fakes, event/action listing, invalid mode, and driver-unavailable doctor. Assert a sentinel password/DSN never appears. Assert there is no `--dsn` argument. Assert `reindex --mode act` fails without both `--confirm` and the environment feature flag.

- [ ] **Step 2: Run CLI tests and verify RED**

```bash
python -m unittest scm_agent.healing.tests.test_cli -v
```

Expected: missing module.

- [ ] **Step 3: Implement CLI and documentation**

Use `argparse`, structured JSON output, clear nonzero exit codes, and graceful Ctrl-C. `doctor` checks audit writability before database capability. `once`/`run` instantiate production adapters lazily. `events`/`actions` are SQLite-only. `requirements-live.txt` contains a bounded Psycopg binary dependency. README explains setup, environment variables, exact safety model, default observe behavior, and truthful “auditable/non-destructive, not reversible” viva language.

- [ ] **Step 4: Write failing demo guard and command-construction tests**

Assert demo commands refuse a non-disposable database unless `SCM_HEALING_ALLOW_DEMO=1`, use only `scm_ovs_demo_*` identifiers/application tags, bloat disables autovacuum only on its disposable table, pressure opens a bounded requested count, and cleanup targets only tagged demo resources.

- [ ] **Step 5: Implement guarded demo helpers**

Provide idempotent setup/hold/cleanup operations for bloat, idle transaction and pressure. Keep the driver lazy. Use `finally`/interrupt cleanup for held connections. Do not put demo DDL in the normal healing engine.

- [ ] **Step 6: Run focused/full tests and commit**

```bash
python -m unittest scm_agent.healing.tests.test_cli -v
python -m unittest scm_agent.healing.tests.test_demo -v
python -m scm_agent.healing --help
python -m unittest discover -s scm_agent/healing/tests -v
git add scm_agent/healing
git commit -m "feat(healing): add operator CLI and guarded demos"
```

---

### Task 7: Integrated verification, state handoff, and final safety review

**Files:**
- Modify: `PROJECT_STATE.md` (announced shared truth file)
- Modify only if tests expose a defect: files under `scm_agent/healing/**`

**Interfaces:**
- The handoff must distinguish dependency-free verification from skipped live PostgreSQL verification.

- [ ] **Step 1: Run the complete dependency-free verification suite**

```bash
python -m unittest discover -s scm_agent/healing/tests -v
python -m compileall -q scm_agent/healing
python -m scm_agent.healing --help
python -m scm_agent.healing events --limit 1
git diff --check
```

Expected: tests pass with live tests reported as skipped if prerequisites are absent; compile/help/audit inspection exit zero; no whitespace errors.

- [ ] **Step 2: Verify ownership and secret invariants**

```bash
git diff --name-only 7baf1fc..HEAD
rg -n "VACUUM FULL|REINDEX INDEX(?! CONCURRENTLY)|DROP INDEX" scm_agent/healing --glob '*.py'
rg -n "postgresql://.*:.*@" scm_agent/healing --glob '*.py' --glob '!tests/*'
```

Manually confirm every non-healing changed source path is either the design/plan or the announced `PROJECT_STATE.md`. Review any regex matches in tests/constants rather than treating text presence alone as a defect.

- [ ] **Step 3: Update the shared truth file**

Append a dated healing-engine section recording implemented modules, exact verification commands/counts, live-test skip reason (`psycopg` absent and Docker requires sudo), default safety posture, and remaining prerequisites. Do not rewrite Claude’s concurrent entries.

- [ ] **Step 4: Commit the handoff**

Stage only the shared state file and any reviewed healing fixes:

```bash
git add PROJECT_STATE.md scm_agent/healing
git commit -m "docs: record healing engine verification state"
```

- [ ] **Step 5: Request final whole-feature review**

Provide the design, this plan, full diff from the baseline, verification output, and any deferred findings to a fresh high-capability reviewer. Fix all critical/important findings through one reviewed fix wave, rerun Step 1, and preserve residual non-blocking rulings in the development ledger.
