# SCM-OVS healing operator guide

The healing engine monitors PostgreSQL catalog statistics, records anomaly
events in a local SQLite journal, and can apply a deliberately small set of
guarded maintenance actions. The default mode is `observe`: it detects and
records findings but does not create or execute action plans.

## Local environment

Use `python3`; this host does not provide a `python` alias. Live PostgreSQL
operations additionally need Psycopg in a project virtual environment. The
bounded live dependency is in `requirements-live.txt`.

On a machine where the required OS packages are available, the intended setup
is:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r scm_agent/healing/requirements-live.txt
```

This repository's development PostgreSQL is exposed on host port **5434**;
ports 5432 and 5433 belong to other projects on the current machine. Start it
from the repository root with the project's Docker Compose configuration:

```bash
sudo docker compose up -d
```

The commands above are operator instructions, not automatic installation
steps. If `python3 -m venv` reports that `venv` is unavailable, install the
matching Python venv OS package from your own terminal first. Do not use
`--break-system-packages` against the system Python.

Driverless commands work without Psycopg:

```bash
python3 -m scm_agent.healing --help
python3 -m scm_agent.healing events
python3 -m scm_agent.healing actions
python3 -m scm_agent.healing doctor
```

`doctor` checks that the SQLite audit journal can be opened before attempting a
database capability check. A missing driver or unreachable database produces
structured JSON and a nonzero exit code rather than exposing a connection
string.

## Configuration

The database DSN is accepted only through `SCM_DB_DSN`. There is intentionally
no `--dsn` option, and structured CLI output never includes the DSN or database
exception text. The development value targets Docker port 5434:

```bash
export SCM_DB_DSN='postgresql://scm:scm@localhost:5434/scm_demo'
export SCM_HEALING_AUDIT_PATH='var/scm_ovs/healing-audit.sqlite3'
```

Operational variables and their conservative defaults:

| Variable | Default | Meaning |
|---|---:|---|
| `SCM_HEALING_MODE` | `observe` | `observe`, `dry_run`, or `act` |
| `SCM_HEALING_POLL_SECONDS` | `30` | daemon poll interval |
| `SCM_HEALING_REQUIRED_CONSECUTIVE` | `2` | distinct detections required before action |
| `SCM_HEALING_MAX_ACTIONS_PER_CYCLE` | `1` | fixed v1 mutation budget |
| `SCM_HEALING_VACUUM_COOLDOWN_SECONDS` | `21600` | minimum six-hour per-table cooldown |
| `SCM_HEALING_LOCK_TIMEOUT_SECONDS` | `5` | live action lock timeout |
| `SCM_HEALING_STATEMENT_TIMEOUT_SECONDS` | `60` | live query/action timeout |
| `SCM_HEALING_PROTECTED_ROLES` | `postgres` | comma-separated roles never terminated |
| `SCM_HEALING_ALLOW_TERMINATE_IDLE_TRANSACTION` | `false` | enables exact-session termination in act mode |
| `SCM_HEALING_ALLOW_MANUAL_REINDEX` | `false` | enables manual concurrent reindex planning |

Rule thresholds are also configurable:

- `SCM_HEALING_DEAD_TUPLE_RATIO`
- `SCM_HEALING_MINIMUM_TABLE_ROWS`
- `SCM_HEALING_MINIMUM_DEAD_TUPLES`
- `SCM_HEALING_IDLE_TRANSACTION_WARNING_SECONDS`
- `SCM_HEALING_IDLE_TRANSACTION_ACTION_SECONDS`
- `SCM_HEALING_LONG_QUERY_SECONDS`
- `SCM_HEALING_CONNECTION_WARNING_RATIO`
- `SCM_HEALING_CONNECTION_CRITICAL_RATIO`
- `SCM_HEALING_UNUSED_INDEX_MINIMUM_BYTES`
- `SCM_HEALING_UNUSED_INDEX_MAXIMUM_SCANS`
- `SCM_HEALING_MINIMUM_STATISTICS_AGE_SECONDS`

Invalid modes, booleans, or weakened v1 safety limits fail before live database
I/O.

## Operating commands

Run one default observe cycle or a continuous loop:

```bash
python3 -m scm_agent.healing once
python3 -m scm_agent.healing run
```

Use `--mode dry_run` to perform capability/identity checks and record the exact
planned command plus before/after snapshots without mutation. Use
`--mode act` only as an explicit operational decision:

```bash
python3 -m scm_agent.healing once --mode dry_run
python3 -m scm_agent.healing run --mode act
```

`Ctrl-C` stops the daemon cleanly with a JSON interruption result. `events` and
`actions` read only the local SQLite journal and never instantiate a PostgreSQL
adapter. Both accept an optional global `--limit` from 1 through 100; the bound
applies across all targets in the deterministic journal listing order:

```bash
python3 -m scm_agent.healing events --limit 20
python3 -m scm_agent.healing actions --limit 20
```

Manual reindex is always `REINDEX INDEX CONCURRENTLY`. A dry run requires the
feature flag. A real run requires both the current environment flag and the
command-line confirmation; neither is persisted as permission for a future
shell:

```bash
export SCM_HEALING_ALLOW_MANUAL_REINDEX=1
python3 -m scm_agent.healing reindex public orders_customer_idx --mode dry_run
python3 -m scm_agent.healing reindex public orders_customer_idx --mode act --confirm
```

## Exact safety model

- `observe` detects and journals only. It is the default.
- `dry_run` performs live precondition checks, writes an audit intent and
  before/after snapshots, and executes no mutation.
- `act` still requires two distinct detections, a current live identity and
  privilege check, a committed audit intent before dispatch, and a maximum of
  one action per cycle.
- Automatic maintenance is limited to online plain `VACUUM` with
  `SKIP_LOCKED` and `TRUNCATE FALSE`. `VACUUM FULL` is never generated.
- Reindex is manual-only and concurrent. System schemas, stale identities,
  unsupported server versions, and missing privileges are rejected.
- Backend termination is off by default and applies only to one exact
  `idle in transaction` session fingerprint. The healer's own backend,
  background workers, superusers, and protected roles are excluded.
- Every action attempt uses bounded lock/statement timeouts. Successful table
  vacuuming is rate-limited by the cooldown journal.
- The monitored PostgreSQL schema is not changed by the normal engine. Its
  append-only audit journal lives in the application-owned SQLite file.

For the viva, describe this as **auditable and non-destructive, not
reversible**. `VACUUM` and `REINDEX` cannot be undone, but they rebuild/reclaim
equivalent storage rather than deleting live data. `pg_terminate_backend`
cannot be undone either: PostgreSQL rolls back that client's open transaction,
so it is the genuinely disruptive action and therefore has the strongest
opt-in and identity guards. Before/after snapshots provide evidence, not a
rollback mechanism.

## Disposable demo scenarios

Demo helpers are separate from the normal engine. They create only the
`scm_ovs_demo_bloat` table and use only `scm_ovs_demo_*` application tags.
Without an override, they run only against a database whose name begins
`scm_ovs_demo_`. The supplied `scm_demo` development database therefore needs
an explicit, current-shell acknowledgement:

```bash
export SCM_HEALING_ALLOW_DEMO=1
```

Set that variable only after confirming `SCM_DB_DSN` identifies a disposable
database. It is not a production safety bypass.

Create deterministic dead tuples (the helper disables autovacuum only on its
own disposable table), then inspect or heal them:

```bash
python3 -m scm_agent.healing demo bloat setup --rows 20000
python3 -m scm_agent.healing once --mode observe
python3 -m scm_agent.healing demo bloat cleanup
```

Hold an idle transaction or a bounded number of pressure connections until
`Ctrl-C` (or supply `--seconds`):

```bash
python3 -m scm_agent.healing demo idle hold
python3 -m scm_agent.healing demo pressure hold --connections 10
python3 -m scm_agent.healing demo idle cleanup
python3 -m scm_agent.healing demo pressure cleanup
```

Connection pressure is restricted to 1–32 requested connections. Held
connections are closed in `finally` on normal completion or interruption.
Cleanup terminates only exact/escaped `scm_ovs_demo_*` application tags and
drops only the exact disposable bloat table. Repeated setup and cleanup are
safe:

```bash
python3 -m scm_agent.healing demo cleanup
```
