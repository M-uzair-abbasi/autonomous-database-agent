# Testing and verification status

Everything in this file is reproducible from a clean checkout. Where something
has **not** been verified, it says so — an untested path reported as working is
worse than a known gap.

---

## Running the suites

No third-party packages are required. Both suites run on the standard library.

```bash
# Pillar 1 — each module carries an executable self-test
python3 -m scm_agent.pipeline.config
python3 -m scm_agent.pipeline.schema_context
python3 -m scm_agent.pipeline.safety
python3 -m scm_agent.pipeline.model_client
python3 -m scm_agent.pipeline.nl_to_sql
python3 -m scm_agent.app.main
python3 -m scm_agent.eval.eval_harness

# Pillar 2 — unittest suite
python3 -m unittest discover -s scm_agent/healing/tests -t .
```

Expected:

```
Pillar 1   7 modules, all exit 0
Pillar 2   Ran 187 tests ... OK (skipped=5)
```

The 5 skips are live-PostgreSQL tests. They are gated behind `SCM_TEST_PG_DSN`,
and the destructive ones additionally require `SCM_TEST_PG_DISPOSABLE=1` plus an
exact match on a disposable database name. They skip with an explicit reason
rather than passing silently.

## Running the API

```bash
docker compose up -d          # PostgreSQL on port 5434

SCM_MODEL_PROVIDER=mock SCM_DIALECT=postgresql \
SCM_DB_DSN='postgresql://scm:scm@localhost:5434/scm_demo' \
python3 -m uvicorn scm_agent.app.main:app --port 8080
```

Interactive documentation at `/docs`. With `SCM_DIALECT=sqlite` and a SQLite
DSN, the API runs with no database server at all.

## Evaluation

```bash
python3 -m scm_agent.eval.eval_harness --out results.json
```

Reports execution accuracy overall, by difficulty, by tag, and by failure
reason, labelled with the active model stage so a result can never be attributed
to the wrong model.

---

## Verification status

### Verified

| Area | Evidence |
|---|---|
| Schema introspection | SQLite — 4 tables, all columns, all 3 foreign keys recovered |
| Read-only enforcement | 42 case suite + 12 independent checks; 73 forged malicious model replies produced zero database mutations |
| Executed SQL == validated SQL | Confirmed by tracing at the database driver level |
| Prompt template fidelity | Byte-identical to the published model card, 0 differences across 25 lines |
| API endpoints | 81 checks, plus live HTTP against a SQLite-backed server |
| `/health` credential safety | A planted password appears nowhere in the response payload |
| Healing mode enforcement | No path found from `observe`/`dry_run` to an executed command |
| Healing action guards | 12 termination attack scenarios blocked, including PID recycling; identifier fuzzing with 30 hostile inputs produced 0 escapes |
| Cooldown durability | Survives process restart; 6 concurrent processes against a live cooldown, 0 acquired |

### Not verified

- **No PostgreSQL server has been reached at any point.** All results above are
  against SQLite or in-process fakes.
- **The PostgreSQL, MySQL and SQL Server introspection paths have never
  executed.** They are implemented; they are not tested. Do not report them as
  working.
- **The `psycopg` connection path is unexercised** — the driver is not installed
  in the development environment.
- **The vLLM client path is unexercised** — no GPU has been available, so the
  base model has never been loaded. All accuracy figures to date come from the
  mock provider and are not model results.

### Known limitations

- One evaluation case (`medium-02`) uses PostgreSQL-only date literal syntax
  that SQLite rejects. The harness reports it as *skipped — reference query
  failed*, excluded from the denominator, never counted as a model error. Once a
  PostgreSQL server is available the set becomes 15/15 gradeable.
- The healing engine aborts a whole collector if `pg_stat_activity` masks a
  column, which occurs when the connected role is neither superuser nor a member
  of `pg_monitor`. Detection then silently returns nothing.
- Monitoring and healing share one connection role; monitoring is not separately
  read-only.
- The package emits no logs, so a failing poll loop is not distinguishable from
  an idle one at the terminal.

---

## Test design notes

**Result-set comparison, not string comparison.** Two different queries that
return the same rows are both correct. Comparing SQL text would mark correct
answers wrong.

**Row order is graded only when the question asks for it.** A reference query
carrying a presentational `ORDER BY` would otherwise impose a hidden strict
requirement and understate accuracy for every model measured.

**Numeric normalisation.** `Decimal("558.00")`, `558.0` and `558` compare equal,
because `NUMERIC` is exact in PostgreSQL and floating point in SQLite.

**The sample data deliberately distinguishes `LEFT JOIN` from `INNER JOIN`.** It
contains one customer with no orders and one product never ordered, so the two
joins return different row counts (13 vs 12). Without those rows the evaluation
set could not detect a model confusing the two.

**The mock model imitates real output shape.** It emits reasoning prose followed
by a fenced code block, matching the real model, so the response parser is
exercised against a realistic reply rather than clean SQL that would never
arrive in practice.
