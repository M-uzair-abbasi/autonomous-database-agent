# GPT_START_HERE.md — read this before doing anything else

You (Codex / GPT) are working in a shared directory alongside Claude Code on a
Final Year Project. This file is your onboarding. Read it fully, then read
`PROJECT_STATE.md`, before you create or edit a single file.

Written by Claude Code, 2026-08-12, for the project owner.

---

## 0. Your brief in one paragraph

**Your lane is Pillar 2: the self-healing database engine**, in
`scm_agent/healing/`. Build monitoring (`pg_stat_activity`,
`pg_stat_user_tables`, `pg_stat_user_indexes`), rule-based anomaly thresholds,
corrective actions (VACUUM / REINDEX / connection termination), and an audit log
with a pre-action snapshot.

Do **not** edit anything under `scm_agent/pipeline/`, `scm_agent/eval/` or
`scm_agent/db/` — Claude Code owns those. Announce before touching
`app/main.py`, `docker-compose.yml` or `requirements.txt`. Do not modify
`pipeline/config.py`'s swap pattern. The self-healing engine is
PostgreSQL-only — ignore the dialect registry. The database runs on **port
5434**, not 5432. `pip install` is blocked on this machine; read section 6
before installing anything.

---

## 1. First, a correction — `AGENTS.md` is wrong about what exists

`AGENTS.md` section 3 claims a working scaffold "already exists (built and
tested)" and lists `schema_context.py`, `model_client.py`, `safety.py`,
`nl_to_sql.py`, `app/main.py`, `eval/eval_harness.py`, `eval/test_set.json` and
`requirements.txt`.

**None of those files exist.** This was verified by scanning this directory and
the whole home directory. `AGENTS.md` describes the intended design, not
completed work. Do not import from those files or assume their behaviour.

What actually exists right now:

| Path | Status |
|---|---|
| `scm_agent/pipeline/config.py` | Built and tested (Claude Code owns it) |
| `scm_agent/db/schema.sql` | Written, not yet executed |
| `docker-compose.yml` | Written, not yet started |
| `PROJECT_STATE.md` | Shared truth file — keep it updated |
| Everything else in `AGENTS.md` §3 | Does not exist |

**There is no git repository here.** An overwrite is unrecoverable. Stay in your
lane, and if the user has run `git init`, commit early and often.

---

## 2. Ownership map

| Owner | Paths |
|---|---|
| **Claude Code** | `scm_agent/pipeline/**`, `scm_agent/eval/**`, `scm_agent/db/**` |
| **You (Codex/GPT)** | `scm_agent/healing/**` |
| **Announce first** | `app/main.py`, `docker-compose.yml`, `requirements.txt`, `CLAUDE.md`, `AGENTS.md`, `PROJECT_STATE.md` |

Pillar 1 (NL→SQL) and Pillar 2 (self-healing) share **zero source files**, so we
can both work in parallel safely. `app/main.py` is the only integration point.

If you must edit a file you don't own: tell the user first. Never silently
rewrite.

---

## 3. The self-healing engine — full spec

This is the project's actual differentiator. The literature review found that
pganalyze, Metabase and D-Bot each lack this combination, so this is what the
FYP is judged on. It matters more than the NL→SQL half.

### 3.1 Boundary

- **PostgreSQL only.** `VACUUM`, `REINDEX` and `pg_stat_*` have no portable
  cross-engine equivalent. The NL→SQL side is multi-dialect; you are not.
  Do not consult `DIALECT_SPECS`.
- **Rule-based detection only.** The approved scope says rule-based thresholds.
  Do not build ML anomaly detection — it is out of scope and adds risk.
- **No dashboard.** The React frontend belongs to a teammate (Person C).

### 3.2 What to monitor, and from where

| Signal | Source | Rough rule |
|---|---|---|
| Table bloat | `pg_stat_user_tables`: `n_dead_tup`, `n_live_tup`, `last_autovacuum` | dead/(live+dead) over a threshold **and** table above a minimum row count |
| Idle-in-transaction | `pg_stat_activity`: `state='idle in transaction'`, age of `state_change` | open beyond N minutes |
| Long-running queries | `pg_stat_activity`: age of `query_start`, `state='active'` | running beyond N minutes |
| Connection saturation | `count(*)` from `pg_stat_activity` vs `max_connections` | above a percentage |
| Unused / bloated indexes | `pg_stat_user_indexes`: `idx_scan`, plus `pg_relation_size` | `idx_scan` near zero on a large index |

Treat thresholds as configurable, not hardcoded. The user will need to tune them
live during the viva demo.

**`idle in transaction` is the one that actually matters.** Those sessions hold
locks and prevent autovacuum from reclaiming dead rows, so they *cause* bloat.
Plain `idle` connections are mostly harmless. Do not treat them the same.

### 3.3 ⚠️ Safety rails — read this before writing any corrective action

An autonomous agent running maintenance commands on a live database can cause a
serious outage. These are not optional.

1. **Never auto-run `VACUUM FULL`.** It takes an `ACCESS EXCLUSIVE` lock and
   rewrites the entire table — all reads and writes block until it finishes,
   which on a large table can be many minutes. Plain `VACUUM` is online and
   safe. If `VACUUM FULL` is ever offered, it must be human-approved only.
2. **Use `REINDEX INDEX CONCURRENTLY`** (PostgreSQL 12+). Plain `REINDEX` blocks
   writes on the table for the duration.
3. **`pg_terminate_backend` needs guards.** Never terminate your own backend
   (`pg_backend_pid()`), never terminate autovacuum workers or background
   workers, and only ever target sessions matching your specific rule. A bug
   here kills your teammates' connections mid-demo.
4. **Privileges.** `VACUUM` requires table ownership or superuser (newer
   PostgreSQL versions add a `MAINTAIN` privilege — check your server version).
   `pg_terminate_backend` requires the `pg_signal_backend` role. Fail with a
   clear message if the connected user lacks these, rather than erroring
   mid-action.
5. **Rate-limit and cool down.** Never VACUUM the same table twice in quick
   succession. A detection loop with no cooldown becomes a self-inflicted DoS.

### 3.4 ⚠️ "Reversible" — be honest about this

`AGENTS.md` promises healing actions are "reversible" with a "pre-action
snapshot for rollback." Taken literally that is **not achievable**, and an
examiner may well ask about it. Get the framing right now:

- **`VACUUM` is not reversible** — but it is also not destructive. It reclaims
  space from dead tuples. There is nothing to undo.
- **`REINDEX` is not reversible** — but it rebuilds an equivalent index. Not
  data-destructive.
- **`pg_terminate_backend` is not reversible** — the client's open transaction is
  rolled back by PostgreSQL. **This is the genuinely destructive action.**

So what you actually build, and what the report should claim:

- a **before/after snapshot** of the relevant statistics for every action,
- a **complete audit record** (timestamp, rule fired, target object, command
  issued, outcome, duration, error if any),
- a **dry-run mode** that logs what it *would* do without doing it.

Describe this as **auditable and non-destructive**, not "reversible." That is
defensible at the viva; "reversible" is not.

### 3.5 ⚠️ Where the audit log lives — architectural constraint

Objective BO-6 promises plug-and-play: connect to **any** Postgres database via
credentials, **with no schema changes**.

That means **the audit log must not be a table you create inside the monitored
database.** Creating one would be a schema change and would break BO-6 — an
examiner will notice.

Store logs in the application's own storage (a separate app database, or a
SQLite file to start). The monitored database is a target, not a host.

### 3.6 Operating modes

Build all three from the start; the demo depends on it:

- **`observe`** — detect and log anomalies, take no action. Default.
- **`dry_run`** — log the exact command it would execute, execute nothing.
- **`act`** — actually heal.

Never make `act` the default. The user must opt in.

### 3.7 Demo / stress testing (do not skip — this is a graded deliverable)

The scope requires deliberately inducing anomalies so healing can be demoed
live. Script these so the viva is repeatable, not improvised:

- **Bloat:** create a table, insert many rows, `UPDATE`/`DELETE` most of them.
  Set `ALTER TABLE ... SET (autovacuum_enabled = false)` on that table first, or
  autovacuum will quietly clean up before your engine notices and the demo
  shows nothing.
- **Idle in transaction:** open a connection, issue `BEGIN`, run a statement,
  never commit. Leave it hanging.
- **Connection pressure:** open many simultaneous connections.

Each scenario should be a single command the user can run on stage, then show
the engine detecting and fixing it.

### 3.8 Out of scope — do not build

- ML-based anomaly detection (scope says rule-based)
- Support for MySQL / SQL Server / MongoDB in the healing engine
- The React dashboard
- Anything in `scm_agent/pipeline/`

---

## 4. Decisions already made — do not relitigate without asking

1. **Multi-dialect SQL is in scope for NL→SQL** (PostgreSQL, MySQL, SQL Server,
   SQLite). The user chose this explicitly. It does not apply to your half.
2. **NoSQL is out of scope** entirely.
3. **Self-healing stays PostgreSQL-only.**
4. **Base model is `seeklhy/OmniSQL-7B`** (Hugging Face, Apache 2.0, not gated).
   It was trained on SQLite-dialect data, so base-model Postgres errors are
   expected; closing that gap is the point of the fine-tune.
5. **Only NL→SQL gets fine-tuned.** Conversational BI stays on API calls.

---

## 5. Invariants — do not break these

1. `pipeline/config.py` is the **single** model swap point. Nothing anywhere may
   hardcode a model name, base URL or provider.
2. All SQL-dialect facts live in `DIALECT_SPECS` in `config.py`.
3. `/health` must never leak the database DSN — it contains a password.
4. The NL query path is read-only. (Your healing path is deliberately *not*
   read-only — that is exactly why it needs the rails in section 3.3.)
5. The inference prompt format is a shared contract with the training-data
   generation. Nobody changes it unilaterally.

---

## 6. Environment facts — this machine

- **No native PostgreSQL.** No `psql`, no `pg_isready`, no `postgresql` service.
- **Port 5432 is occupied** by an unrelated project's container. Port 5433 is
  claimed by the `burk-scm` project. **This project uses 5434.**
- **Docker needs `sudo`** — the user is not in the `docker` group.
- **`pip install` is blocked** by PEP 668 (Ubuntu 24.04, externally managed
  environment). `python3.12-venv` is **not installed**, so `python3 -m venv`
  produces a broken venv with no pip. **Do not work around this with
  `--break-system-packages`** — it risks the system Python and would affect the
  user's other projects. The correct fix is
  `sudo apt install -y python3.12-venv`, which needs the user's password.
- **`sqlite3` works** (stdlib, 3.45.1) — useful for zero-install testing.
- Python 3.12.3.

Connection details once the container is up (disposable dev credentials):

```
host=localhost port=5434 dbname=scm_demo user=scm password=scm
```

## 7. Open blockers (need the user, not you)

1. `sudo apt install -y python3.12-venv` — required before any Postgres driver.
2. `sudo docker compose up -d` — starts the database on port 5434.
3. `git init` — strongly recommended before two agents write in parallel.

Until (1) and (2) are done there is **no reachable PostgreSQL** for you to
develop against. You can still write and unit-test rule logic against fixture
data in the meantime — design the detection rules as pure functions over rows
so they are testable without a live database.

---

## 8. How the user wants to be worked with

Copied from `AGENTS.md` §6, and it applies to you:

- **Go step by step.** Do not generate the whole engine in one shot — build one
  piece, show it actually running and tested, then move on.
- **Explain what you are doing as you go.** They are a student and want to
  understand it, not receive a black box.
- **Always test before declaring something done.** Show real output.
- **Flag conflicts with the approved scope or the timeline early**, not at the
  viva.

Keep `PROJECT_STATE.md` updated as you complete work, so Claude Code and any
future session can see what is real.
