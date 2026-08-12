# SCM-OVS — Architecture

Autonomous AI Database Agent. Two independent subsystems share one repository
and one FastAPI surface, but no source files.

| Pillar | Package | Purpose |
|---|---|---|
| 1 — Natural language to SQL | `scm_agent/pipeline/` | Translate plain-English questions into SQL against any schema, enforce read-only, execute, return results |
| 2 — Self-healing engine | `scm_agent/healing/` | Monitor PostgreSQL health, detect anomalies by rule, apply a small set of guarded maintenance actions, journal everything |

The two pillars share no modules. `scm_agent/app/main.py` is the only
integration point.

---

## Pillar 1 — natural language to SQL

```
question
   -> schema_context.introspect()      read the live catalog
   -> nl_to_sql.build_prompt()         schema-aware prompt
   -> model_client.generate()          mock | vLLM | fine-tuned adapter
   -> nl_to_sql.extract_sql()          parse SQL out of the reply
   -> safety.check_sql()               read-only enforcement
   -> execute                          only if allowed
```

### `pipeline/config.py` — the model swap point

One environment variable moves the whole system between three stages. Nothing
else in the codebase hardcodes a model name, base URL or provider.

| Stage | `SCM_MODEL_PROVIDER` | Extra | Reported as |
|---|---|---|---|
| 1 | `mock` | — | `stage-1-mock` |
| 2 | `openai_compatible` | `SCM_MODEL_BASE_URL` | `stage-2-base` |
| 3 | `openai_compatible` | + `SCM_LORA_ADAPTER` | `stage-3-finetuned` |

vLLM serves a LoRA adapter alongside the base weights under its own model name,
so stage 2 → 3 is a configuration change rather than a code change. The active
stage is reported on `/health` so it cannot be misrepresented during a
demonstration.

This file also holds `DIALECT_SPECS`, the single registry of per-dialect facts
(prompt name, identifier quoting, comment tokens). Dialect knowledge lives here
and nowhere else.

### `pipeline/schema_context.py` — schema independence

Introspects a live database — tables, columns, primary keys, foreign keys — and
renders `CREATE TABLE` DDL for the prompt. The schema is read on every request
and never baked into the model, which is what allows the system to work against
a database it has never seen without retraining (objective BO-6).

Optionally embeds a few real column values as SQL comments
(`status VARCHAR(20), -- e.g. delivered, shipped, pending`). This measurably
reduces a common failure mode where the model emits syntactically valid SQL that
matches no rows because it guessed the wrong literal casing. Off by default,
since it puts real values into model context.

### `pipeline/safety.py` — read-only enforcement

Every generated statement is checked before execution. Only `SELECT` and
`WITH … SELECT` are permitted. The checker is dialect-aware because comment
syntax differs — MySQL treats `#` as a line comment and the others do not, which
is a viable place to hide a write.

Handled explicitly: data-modifying CTEs (`WITH x AS (INSERT … RETURNING *)`),
`SELECT … INTO`, stacked statements, comment-based evasion, string literals
containing keywords, and PostgreSQL dollar-quoting (`$$…$$`), where a naive
scanner treats an embedded apostrophe as a delimiter and can skip past an
injected statement.

The executed string is the checker's `normalized_sql`, never the raw model
output — validating one string and running another is the usual way such a layer
is defeated.

### `pipeline/nl_to_sql.py` — orchestration

Owns `OMNISQL_PROMPT_TEMPLATE`, reproduced verbatim from the published model
card with only the `Database Engine:` line parameterised per dialect.

**This template is a contract.** Fine-tuning data must be generated in the same
format the system uses at inference time. If the two diverge the fine-tuned
model underperforms silently, with no error to diagnose. Do not change the
prompt builder once training-data generation has started.

### `app/main.py` — HTTP surface

`GET /health`, `GET /schema`, `POST /query`. All endpoint logic lives in
framework-free functions returning plain dictionaries; the web routes are thin
wrappers, so the API is testable without starting a server. `/health` never
includes the DSN, which carries a password.

### `eval/` — measurement

`test_set.json` holds 15 graded questions (5 easy, 6 medium, 4 hard).
`eval_harness.py` computes execution accuracy by running the candidate SQL and
the reference SQL against the same database and comparing **result sets**, not
query text — two different queries returning the same rows are both correct.

Row order is graded only where the question actually asks for an ordering.
Numeric types are normalised before comparison, since `NUMERIC` is exact in
PostgreSQL and floating point in SQLite. Failures are bucketed by cause — parse
failure, safety denial, execution error, result mismatch — which is what drives
error analysis.

---

## Pillar 2 — self-healing engine

PostgreSQL only, by design: `VACUUM`, `REINDEX` and the `pg_stat_*` views have
no portable cross-engine equivalent. This pillar does not consult the dialect
registry.

```
poll -> collect catalog statistics -> rule-based detection
     -> plan a typed action        -> audit intent
     -> execute under guards       -> audit outcome
```

### Operating modes

| Mode | Behaviour |
|---|---|
| `observe` | Detect and record only. **Default.** |
| `dry_run` | Record the action that would be taken; execute nothing |
| `act` | Execute |

Enforced at three independent layers: the planner refuses to build a plan in
`observe`, the gateway rejects any plan/config mode mismatch, and `execute`
returns a simulated result for `dry_run` before touching anything.

### Detection

| Signal | Source |
|---|---|
| Dead-tuple pressure | `pg_stat_user_tables` |
| Idle in transaction | `pg_stat_activity` |
| Long-running queries | `pg_stat_activity` |
| Connection saturation | `pg_stat_activity` vs `max_connections` |
| Unused / large indexes | `pg_stat_user_indexes` |

Rule-based thresholds, configurable. Ages are computed from the server's
`clock_timestamp()` rather than the client clock, so clock skew cannot trigger
an action.

### Actions and their guards

Only three actions exist, and each is deliberately narrow.

- **`VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE)`** — online only. `VACUUM FULL`
  is never generated: it takes an `ACCESS EXCLUSIVE` lock and rewrites the whole
  table. `TRUNCATE FALSE` suppresses even the brief exclusive lock that plain
  `VACUUM`'s truncation phase would take.
- **`REINDEX INDEX CONCURRENTLY`** — manual, explicitly confirmed, never
  automatic. Non-concurrent `REINDEX` blocks writes.
- **`pg_terminate_backend`** — off by default, requires `act` mode plus an
  explicit configuration flag. The statement re-asserts all eight identity
  fields in the same query that calls the function, so nothing can change
  between check and termination. It cannot target its own backend, a
  non-`client backend` (autovacuum worker, walsender, replication worker), a
  superuser session, a protected role, or a session that is no longer idle in
  transaction.

Identifiers are quoted by doubling embedded quotes, which is the complete rule
for PostgreSQL. Commands are rebuilt from typed targets at execution time, so a
tampered stored plan cannot execute.

Object identity is re-verified immediately before acting. A table OID that has
been recycled onto a different relation, or a PID recycled onto a different
session, is rejected rather than acted upon.

### Rate limiting

A successful action starts a cooldown of at least six hours per target; the
configuration refuses a shorter value. A failed action starts a short backoff
instead — long enough to prevent an expensive statement being reissued every
poll interval, short enough that a transient failure retries soon.
Precondition rejections (insufficient privilege, stale identity) block nothing,
since they never touch the heap.

Cooldowns live in the journal, not in memory, so they survive a restart, and the
check and the intent record share one transaction so concurrent pollers cannot
both act.

### Audit journal

An append-only SQLite journal, **outside the monitored database**. This is
required by objective BO-6: creating an audit table inside the customer database
would be a schema change, which the plug-and-play promise rules out.

Intent is recorded and committed *before* execution, so a crash mid-action
remains visible. If the journal write fails, the action does not proceed. The
DSN is stored only as a SHA-256 hash.

### On "reversible"

Healing actions are **auditable and non-destructive**, not reversible. `VACUUM`
cannot be undone — it also destroys nothing, since it reclaims space from dead
tuples. `REINDEX` rebuilds an equivalent index. `pg_terminate_backend` ends a
session and PostgreSQL rolls back its open transaction; that is the one
genuinely disruptive action, and it is disabled by default.

What exists instead of rollback: a before/after statistics snapshot, a complete
audit record, and a dry-run mode. Snapshots are evidence, not a rollback
mechanism.

---

## Environment

| Component | Value |
|---|---|
| Python | 3.12 |
| Development PostgreSQL | host port **5434** (5432 and 5433 belong to unrelated projects on the development machine) |
| Database / user | `scm_demo` / `scm` (disposable) |
| Model serving | vLLM, OpenAI-compatible endpoint |
| Base model | `seeklhy/OmniSQL-7B` (Apache 2.0) |

Start the database from the repository root:

```bash
docker compose up -d
```

See `docs/TESTING.md` for how to run the suites and for the current verification
status, including which paths have not yet been exercised against a live server.
