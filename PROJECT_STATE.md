# PROJECT_STATE.md — read this before writing any file

Two AI agents (Claude Code and Codex/GPT) share this working directory.
`CLAUDE.md` and `AGENTS.md` are the same original project brief. **They describe
the plan, not reality.** This file records what is actually true right now.

Last verified: 2026-08-12.

---

## 1. Correction to the brief — important

Section 3 of `CLAUDE.md` / `AGENTS.md` claims a working scaffold "already
exists (built and tested)", listing `schema_context.py`, `model_client.py`,
`safety.py`, `nl_to_sql.py`, `app/main.py`, `eval/eval_harness.py`,
`eval/test_set.json`, and `requirements.txt`.

**When this file was first written, none of those files existed** — verified by
directory scan of this machine and of `$HOME`. Section 3 of the brief described
the intended design, not completed work.

**As of 2026-08-12 they now exist and are tested** — see the table below. The
brief is still wrong about *when*, so do not treat it as a record; treat this
file as the record.

A git repository now exists (Codex ran `git init`; commits are healing-side).

## 2. What actually exists right now

| Path | Status |
|---|---|
| `scm_agent/pipeline/config.py` | **Built and tested.** Model swap point + dialect registry. Self-test passes. |
| `scm_agent/pipeline/schema_context.py` | **Built. Verified on SQLite only.** Introspects catalog -> `CREATE TABLE` DDL for the prompt's `{db_details}`, with optional sample values as comments. PostgreSQL / MySQL / SQL Server paths are written but **not yet run against a real server**. |
| `scm_agent/pipeline/safety.py` | **Built and tested.** Read-only enforcement, dialect-aware. 42 self-test cases + 12 independent checks by Claude, all passing. A real PostgreSQL dollar-quoting bypass was found and fixed during development — see AGENT_MAILBOX.md. |
| `scm_agent/pipeline/model_client.py` | **Built and tested.** Mock + openai_compatible providers. Mock deliberately emits reasoning prose then a ```sql fence, matching real OmniSQL output shape. |
| `scm_agent/eval/test_set.json` | **Built and verified.** 15 cases (5 easy / 6 medium / 4 hard). All reference queries execute; 14 verified on SQLite, 1 uses PostgreSQL-only `DATE '...'` syntax and awaits a real server. |
| `scm_agent/db/schema.sql` | **Built, not yet executed against PostgreSQL.** Sample e-commerce schema + synthetic seed data. **Amended 2026-08-12:** added one customer with no orders and one never-ordered product, so `LEFT JOIN` and `INNER JOIN` produce different results (13 vs 12 rows). Without this the eval set could not detect a model confusing the two. |
| `docker-compose.yml` | **Built, NOT started.** Dedicated Postgres on host port **5434**. Blocked: `sudo` cannot prompt for a password in this environment. |
| `scm_agent/pipeline/nl_to_sql.py` | **Built and tested.** Full orchestration. Prompt verified byte-identical to OmniSQL's published template (0 differences across 25 lines). Executes the safety layer's `normalized_sql`, confirmed by driver-level tracing. |
| `scm_agent/app/main.py` | **Built and tested.** `/health`, `/schema`, `/query`. 81 checks pass; verified over live HTTP. Logic is framework-free, FastAPI is a thin wrapper. |
| `scm_agent/eval/eval_harness.py` | **Built and tested.** Execution-accuracy scoring with by-difficulty, by-tag and by-failure-reason breakdowns. Mock baseline **7.1%** (honest, untuned). |
| `scm_agent/{__init__,pipeline/__init__,app/__init__,eval/__init__}.py` | Empty package markers. |

### Verification honesty

Everything above is verified **against SQLite only**. No PostgreSQL server has
been reachable at any point. The PostgreSQL, MySQL and SQL Server introspection
paths in `schema_context.py` are written but have never executed. Do not report
them as working.

### OmniSQL prompt template — verified from the model card

The `Database Schema:` section takes `{db_details}` = `CREATE TABLE` statements,
"optionally including database values and column descriptions via SQL comments".
`schema_context.to_ddl()` emits exactly that. `Database Engine:` is hardcoded to
`SQLite` in the stock template; we parameterise it from `DialectSpec.prompt_name`.
Do not change this format once training-data generation starts (train/serve skew).

## 3. Decisions made after the brief was written

These supersede or extend `CLAUDE.md` / `AGENTS.md`. Do not relitigate without
asking the user.

1. **Multi-dialect SQL is in scope.** The NL→SQL path targets PostgreSQL,
   MySQL, SQL Server and SQLite — not Postgres alone. The user chose this
   explicitly over a Postgres-only build.
2. **NoSQL is out of scope.** MongoDB / Neo4j / Redis are excluded. Different
   query languages, different safety model, no equivalent healing actions.
3. **The self-healing engine stays PostgreSQL-only.** `VACUUM`, `REINDEX` and
   `pg_stat_*` have no portable cross-engine equivalent. Pillar 2 does not
   consult the dialect registry.
4. **Base OmniSQL is SQLite-trained.** Confirmed from the model card: SynSQL-2.5M
   was synthesised in the SQLite dialect, and the stock prompt hardcodes
   `Database engine: SQLite`. Expect base-model dialect errors on Postgres
   (`strftime`, casts, `ILIKE`, `RETURNING`). Closing that gap is the point of
   the fine-tune, and the baseline number is a headline result for the report —
   measure base-model accuracy **before** fine-tuning.
5. **Model source:** `seeklhy/OmniSQL-7B` on Hugging Face, Apache 2.0, not
   gated. 14B/32B exist at the same namespace. A LoRA adapter is tied to the
   base model size — a 7B adapter will not load on 14B.

## 4. File ownership — avoid clobbering each other

This directory has **no version control**, so an overwrite is unrecoverable.
Stay inside your lane unless the user reassigns it.

| Owner | Paths |
|---|---|
| **Claude Code** | `scm_agent/pipeline/**`, `scm_agent/eval/**`, `scm_agent/db/**` |
| **Codex / GPT** | `scm_agent/healing/**` (Pillar 2: monitoring, anomaly rules, corrective actions, audit log) |
| **Announce before editing** | `app/main.py`, `docker-compose.yml`, `requirements.txt`, `CLAUDE.md`, `AGENTS.md`, this file |

Pillar 1 (NL→SQL) and Pillar 2 (self-healing) share no source files, so both can
proceed in parallel with zero conflicts. `app/main.py` is the only integration
point — whoever touches it should say so first.

If you must edit a file you do not own: tell the user, do not silently rewrite.

## 5. Invariants — do not break these

1. **`pipeline/config.py` is the single model swap point.** One env var moves
   the system mock → base OmniSQL on vLLM → fine-tuned LoRA. Nothing else may
   hardcode a model name, base URL or provider. `CLAUDE.md` §6 protects this
   pattern explicitly.
2. **All dialect facts live in `DIALECT_SPECS` in `config.py`.** Do not scatter
   dialect string literals across modules.
3. **`/health` must never leak the DSN.** It carries a password. `summary()` is
   already tested for this.
4. **The NL query path is read-only.** Safety enforcement must be dialect-aware
   — MySQL treats `#` as a line comment, which is a real injection surface the
   other dialects do not have.
5. **Prompt format is a shared contract.** The exact prompt built for inference
   must match the format the training data is generated in. If these diverge,
   the fine-tune silently underperforms (train/serve skew). Nobody changes the
   prompt builder unilaterally once teammates start generating data.

## 6. Environment facts (this machine)

- **No native PostgreSQL.** No `psql`, no `pg_isready`, no `postgresql` service.
- **Host port 5432 is occupied** by an unrelated project's Postgres container.
  Port 5433 is claimed by the `burk-scm` project. **This project uses 5434.**
- **Docker requires `sudo`** — the user is not in the `docker` group.
- **`pip install` is blocked** for NEW packages by PEP 668 (Ubuntu 24.04,
  externally managed). `python3.12-venv` is **not installed**, so
  `python3 -m venv` produces a broken venv with no pip. Do **not** work around
  this with `--break-system-packages`; it risks the system Python. The fix is
  `sudo apt install -y python3.12-venv`, which needs the user's password.
- **CORRECTION (2026-08-12):** an earlier version of this file said no
  third-party packages were available. That was wrong — it generalised from
  `psycopg` failing without checking the rest. Actual state of
  `~/.local/lib/python3.12/site-packages`, verified by import:

  | Package | State |
  |---|---|
  | `fastapi` | **INSTALLED** 0.135.1 |
  | `uvicorn` | **INSTALLED** 0.41.0 |
  | `pydantic` | **INSTALLED** 2.12.5 |
  | `psycopg` | not installed |
  | `openai` | not installed |
  | `pytest` | not installed |

  So **the API layer is not blocked and the server runs today** against SQLite.
  Only the PostgreSQL driver and the vLLM client are genuinely missing.
- **`sqlite3` works** (stdlib, v3.45.1) — usable for testing with zero installs.
- Python 3.12.3.

## 7. Open blockers (need the user)

1. `sudo apt install -y python3.12-venv` — required before any Postgres driver.
   Both `sudo` attempts so far failed with "a terminal is required to read the
   password"; the owner must run these in their own terminal.
2. `sudo docker compose up -d` — starts this project's database on 5434.
3. ~~`git init`~~ — **done** by Codex. But Pillar 1's files are still
   **untracked**; `git clean -fd` would delete them. The owner has been asked to
   commit. Do not `git add -A` Pillar 1 into a healing commit.

## 8. Adversarial review, 2026-08-12 — findings and fixes

An independent agent attacked the whole pipeline. Clean results worth recording:
**zero database mutations across 73 forged malicious model replies**, across all
four dialects; the executed string always equalled the validated string
(confirmed by driver-level tracing); the `skipped-reference-failed` bucket cannot
be abused to inflate accuracy.

Defects found and **fixed**:

- **H1 `test_set.json`** — 10 of 15 reference queries carried an `ORDER BY` the
  question never asked for. Because the harness grades row order whenever the
  reference is ordered (the Spider convention), this was a hidden strict rule
  that would have *understated* accuracy for every model measured. Removed from
  the 10 cosmetic cases; `medium-03` and `hard-01` keep theirs because those
  questions genuinely ask for a ranking. Row order is now graded on 2 of 15.
- **M2 `nl_to_sql._clean_sql_body`** — used `str.strip("```")`, which strips the
  *character set* and ate the closing backtick of MySQL quoted identifiers,
  turning a valid query into "unterminated quoted identifier". Invisible to
  `safety.py`'s tests because those call `check_sql` directly. Now strips only
  complete fence markers.
- **M1/M4 `nl_to_sql._cut_at_statement_end`** — was unaware of comments, so a
  semicolon inside `-- ...` truncated a query mid-clause and returned a *wrong
  answer reported as success*. It also cut stacked statements before the safety
  layer saw them, producing a clean `allowed=True` audit record for a genuine
  injection attempt. Now comment-aware, and returns the whole reply when another
  statement follows the semicolon so the denial is recorded correctly.

Known, **not** fixed (deliberate, for the owner to decide):

- **L1** `AppConfig.summary()` publishes `base_url` unredacted. The DSN is
  correctly excluded, but `SCM_MODEL_BASE_URL=http://user:pass@host/v1` would put
  that password on `/health`. Unlikely with vLLM (it ignores auth), and
  `config.py` is a protected file, so this was left alone rather than changed
  without asking.
- **L5** `eval_harness.execute_sql` runs the *reference* SQL with no safety gate.
  Harmless against a throwaway in-memory SQLite, but becomes a live risk if
  `open_evaluation_database()` is repointed at a real PostgreSQL, since
  `test_set.json` is unreviewed data. Review before that switch.

## 9. PostgreSQL self-healing engine, verified 2026-08-12

Pillar 2 is implemented under `scm_agent/healing/`. It includes typed health
models and configurable rules, a conservative action planner, an append-only
external SQLite audit journal, a lazy Psycopg PostgreSQL gateway, the resilient
cycle engine, an operator CLI, guarded disposable anomaly demos, and operator
documentation. It does not modify the monitored database schema.

The monitored signals are dead-tuple pressure (a catalog-statistics proxy, not
an exact physical-bloat measurement), idle-in-transaction sessions, long active
queries, cluster-wide connection saturation, and large unused indexes. Long
queries, saturation, and unused indexes are advisory only. The only automatic
maintenance action is online plain `VACUUM`; exact idle-transaction termination
requires its own disabled-by-default flag. Concurrent index rebuild is manual,
disabled by default, and requires both the current flag and explicit
confirmation.

Safety posture:

- `observe` is the default mode; `dry_run` executes no mutation; `act` is an
  explicit opt-in.
- An anomaly must appear in at least two consecutive completed collections,
  only one action may be dispatched per cycle, and successful vacuum actions
  have a six-hour target cooldown. A real vacuum with an uncertain outcome is
  conservatively held by the same cooldown instead of being blindly repeated.
- Action intent is committed before database dispatch. Typed database, relation,
  and backend identities are revalidated at execution time. Runtime mode and
  feature flags are checked again by the gateway before any database I/O.
- Plain `VACUUM FULL`, blocking `REINDEX`, and automatic index changes are not
  permitted. Manual rebuild uses only `REINDEX INDEX CONCURRENTLY`; its
  database, index, and parent-table OIDs are bound during authorization and
  revalidated before and after the execution lock.
- The audit journal stores a target hash rather than the DSN, screens free-form
  SQL and credentials, records before/after evidence, and reconciles uncertain
  planned/executing attempts without blindly replaying them.
- Demo commands accept only unambiguous targets and verify
  `pg_catalog.current_database()` on every new connection before demo SQL. Demo
  objects and application tags use the `scm_ovs_demo_` prefix.
- The truthful project claim is **auditable and non-destructive, not
  reversible**. Backend termination is disruptive because PostgreSQL rolls
  back that client's open transaction.

Dependency-free verification completed from the repository root:

```text
python3 -m unittest discover -s scm_agent/healing/tests -v
  Ran 185 tests; OK (skipped=5)
python3 -m compileall -q scm_agent/healing
  exit 0
python3 -m scm_agent.healing --help
  exit 0 without importing Psycopg
SCM_HEALING_AUDIT_PATH=<temporary>/audit.sqlite3 \
  python3 -m scm_agent.healing events --limit 1
  exit 0; {"command": "events", "targets": []}
git diff --check
  exit 0
```

The final independent whole-feature fix re-review returned **Spec PASS** and
**Quality PASS**, with no remaining Critical or Important blocker in the
dependency-free implementation.

The five skips are intentional opt-in PostgreSQL live tests. They cover catalog
collection/observe behavior and disposable `VACUUM`, exact backend termination,
and concurrent reindex actions. They did not run because `psycopg` is absent and
the PostgreSQL Docker service on port 5434 cannot be started without the owner's
`sudo` access. Unit, state-machine, SQL-shape, concurrency, redaction, CLI, and
connection-boundary tests used only the Python standard library and fakes.

Remaining owner prerequisites for live verification:

1. Install `python3.12-venv`, create/repair the project venv, and install only
   `scm_agent/healing/requirements-live.txt` for the Psycopg live adapter.
2. Start this project's Docker Compose PostgreSQL on host port 5434.
3. Run the read-only live tests first. Run disposable action tests only with
   their exact environment guards against a database named `scm_ovs_demo_*`.
