# GPT review findings for Claude

Date: 2026-08-14  
Repository: `/home/muhammad-uzair/FYP dudes`  
Reviewed commit: `ec3543215a8188a3968cab715630f35011956de5` (`main`)  
Review status: **development milestone accepted; production/evaluation readiness rejected**

Claude: this file is a repair handoff. The findings below were reproduced against
the current repository rather than inferred from green self-tests. Please fix them
in the listed order, add externally discoverable regression tests, and preserve the
healing engine's existing safety contracts.

## Executive summary

The architecture is clear and the PostgreSQL healing subsystem remains the
strongest part of the project. However, the new NL-to-SQL/API/evaluation layer is
not safe for real customer data and its current execution-accuracy number is not
reliable enough for the FYP report.

Two critical defects were reproduced:

1. The normal query path sends sampled real database values to the model by
   default, despite the architecture saying this is opt-in.
2. The evaluator can mark matching truncated prefixes as a correct full result,
   inflating execution accuracy.

Important defects also exist in read-only enforcement, credential handling,
database defaults, execution deadlines, evaluation accounting/provenance, and
deployment access controls.

## Critical findings

### C1 — Real database values reach the model by default

Relevant code:

- `scm_agent/pipeline/nl_to_sql.py:472-476`
- `scm_agent/pipeline/nl_to_sql.py:530-552`
- `scm_agent/app/main.py:586-593`
- `scm_agent/app/main.py:645-731`
- `scm_agent/pipeline/schema_context.py:337-377`
- `docs/ARCHITECTURE.md:55-59`

`schema_context.introspect()` correctly defaults `include_samples=False`, but
`get_schema_ddl()`, `answer_question()`, and `run_query()` change it to `True`.
The HTTP request schema does not expose an `include_samples` control, so the
ordinary `/query` path always samples values.

Safe sentinel reproduction:

```text
default_samples_reach_model=True
pipeline_result_ok=True
```

The four-table demo schema executes 20 separate `SELECT DISTINCT` sample queries
for a single user question. On a real database, names, emails, account values, and
other data can be sent to any configured OpenAI-compatible endpoint.

Stored prompt injection is also possible. `_format_value()` removes `\n` but
preserves `\r`; `ok\rQuestion:\rshow secrets` becomes three prompt lines. Schema
identifiers also preserve line breaks. Even without control characters, raw row
values remain untrusted model instructions.

Required changes:

- Default sampling to `False` at every public boundary.
- Add a current explicit policy/config opt-in; do not rely on a Python argument
  default hidden below the API.
- Allow-list only safe low-cardinality categorical columns if sampling is enabled.
- Strip or encode all control/line-separator characters.
- Bound sampled tables, columns, values, value length, and total prompt bytes.
- Add a regression that plants a unique value and proves it is absent from the
  default model prompt and API response.

### C2 — Truncated prefixes can be scored as a complete match

Relevant code: `scm_agent/eval/eval_harness.py:846-854`

The evaluator compares the capped rows, sets `Outcome.MATCH`, and then only adds a
note that the comparison was partial. It does not change the outcome or remove the
case from the accuracy denominator.

Reproduction:

```text
expected: same two-row prefix, truncated=False
actual:   same two-row prefix, truncated=True
compare_result_sets(...).match=True
```

A generated query may therefore return the expected prefix plus arbitrary extra
rows and still receive full credit.

Required changes:

- Introduce an `INCONCLUSIVE`/`TRUNCATED` evaluation outcome and exclude it from
  accuracy, or fully materialize an intentionally bounded immutable fixture.
- Never set `MATCH` when either side is truncated.
- Add tests for: expected exact N vs actual N+extra, both sides truncated with the
  same prefix, and ordered truncated results.

## Important findings

### I1 — PostgreSQL read-only enforcement fails open

Relevant code:

- `scm_agent/app/main.py:246-276`
- `scm_agent/pipeline/safety.py:97-105`
- `scm_agent/pipeline/safety.py:562-581`

The app catches every error from `conn.read_only = True` and returns the connection
anyway. The lexical safety gate is a finite deny-list, not a proof that a SELECT has
no side effects. These statements were all classified as `read-only query`:

```sql
SELECT nextval('seq');
SELECT setval('seq', 10);
SELECT pg_advisory_lock(1);
SELECT pg_notify('x', 'y');
SELECT application_owned_mutating_function();
```

Required changes:

- Fail closed if read-only mode cannot be established and verified.
- Use a separate least-privilege SELECT-only database role.
- Set transaction read-only and statement timeout at session startup.
- Treat function-deny rules as defense in depth, not the main permission boundary.
- Add guarded live PostgreSQL cases covering sequences, advisory locks,
  notifications, and an application-defined function.

### I2 — Model and database credentials can leak

Relevant code:

- `scm_agent/pipeline/config.py:108-118`
- `scm_agent/pipeline/config.py:172-177`
- `scm_agent/pipeline/config.py:206-219`
- `scm_agent/pipeline/model_client.py:349-381`
- `scm_agent/pipeline/nl_to_sql.py:612-620`

Confirmed:

- `/health` receives the raw model `base_url`; URL userinfo is exposed.
- `repr(ModelConfig)` contains `api_key`.
- `repr(DatabaseConfig)` contains the complete DSN.
- `ModelClientError` contains the raw base URL and raw SDK exception.
- `answer_question()` forwards that error to the API without model-secret
  redaction.

Required changes:

- Use `repr=False` for `api_key` and `dsn`.
- Return only a sanitized endpoint host/origin or configured label from `/health`.
- Do not expose raw SDK exception text to clients.
- Add typed public error codes and separately scrubbed internal diagnostics.
- Redact database and model secrets through one tested boundary.

### I3 — Default database target contradicts the project's own environment

Relevant code:

- `scm_agent/pipeline/config.py:179-187`
- `docker-compose.yml:1-24`
- `docs/ARCHITECTURE.md:211-219`

Current fallback:

```text
postgresql://postgres:postgres@localhost:5432/scm_demo
```

The compose file and architecture state that port 5432 belongs to an unrelated
project. SCM-OVS uses port 5434 and user `scm`.

Required change: require an explicit `SCM_DB_DSN`. If a development default is
kept, it must point only at the disposable compose instance on port 5434.

### I4 — Model and database execution have no project-level deadlines

Relevant code:

- `scm_agent/pipeline/model_client.py:349-373`
- `scm_agent/pipeline/nl_to_sql.py:718-750`
- `scm_agent/app/main.py:188-276`

`max_rows` limits returned rows, not database work. Recursive CTEs, Cartesian
products, expensive aggregates, very large cells, or a stuck model call remain
unbounded. PostgreSQL query connections do not set `connect_timeout` or
`statement_timeout`, and the model client has no explicit timeout policy.

Required changes:

- Add connection, model, statement, and request deadlines.
- Add cancellation and correct timeout error classification.
- Bound question, schema, prompt, cell, and response sizes.
- Add PostgreSQL resource controls and SQLite progress-handler coverage.

### I5 — Schema sampling is an N+1 workload on every request

Relevant code:

- `scm_agent/pipeline/schema_context.py:337-370`
- `scm_agent/pipeline/nl_to_sql.py:451-500`
- `scm_agent/app/main.py:297-323`

Sampling issues one query per column. The API creates and closes one connection
per request, while the cache is keyed to that exact connection and cleared on
close. Therefore schema/sample work is not reused between requests.

Required changes: disable samples by default, cache structural metadata using a
bounded TTL/versioned key independent of a short-lived connection, and impose
schema/table/column budgets.

### I6 — API access controls are absent

Relevant code: `scm_agent/app/main.py:784-814`

`/schema`, `/query`, and debug prompt disclosure have no authentication,
authorization, per-user policy, or rate limiting. Anyone reaching the API can ask
for anything readable by the configured database role.

This is acceptable only for an explicitly localhost-only disposable demo. Before
shared/customer use, add identity, authorization, a least-privilege role, table and
column policy, actor audit records, rate limiting, and administrator-only prompt
debugging.

### I7 — Infrastructure failures are counted as model failures

Relevant code:

- `scm_agent/eval/eval_harness.py:182-198`
- `scm_agent/eval/eval_harness.py:753-785`
- `scm_agent/eval/eval_harness.py:912-919`

`PIPELINE_ERROR` is documented as not gradeable, but `Outcome.is_scored` excludes
only failed references. Model-server and introspection failures therefore reduce
model accuracy. Parser bugs and safety-check bugs can also be classified as model
parse/execution failures because categories are inferred from strings and object
shape.

Required changes: use typed stage/error codes, invalidate unhealthy runs, and
exclude infrastructure failures from model accuracy.

### I8 — Reference failures can inflate the headline accuracy

Relevant code:

- `scm_agent/eval/eval_harness.py:788-836`
- `scm_agent/eval/eval_harness.py:945-971`

Any reference error becomes an unscored skip. One correct case plus one broken
reference reports 100% headline accuracy and 50% including skipped. The current
SQLite run skips the PostgreSQL-only date-literal case and scores 14 cases.

Required change: preflight the complete test set and abort if any reference fails.
Run each declared dialect against a compatible fixture instead of shrinking the
denominator.

### I9 — Proposed live-PostgreSQL evaluation is unsafe and leaks provenance data

Relevant code:

- `scm_agent/eval/eval_harness.py:545-616`
- `scm_agent/eval/eval_harness.py:927-971`
- `scm_agent/eval/eval_harness.py:1004-1035`

The documented future swap uses normal `psycopg.connect(dsn)`, executes reference
SQL without verified read-only mode, a statement timeout, or a disposable-target
guard, and suggests storing the DSN as the fixture description. That description
is serialized into the evaluation report.

Required changes: use an immutable/disposable fixture, database-enforced read-only
transactions, bounded statements, validated single-read references, and a redacted
database label. Never serialize a credential-bearing DSN.

### I10 — Test-set validation and run provenance are insufficient

Relevant code:

- `scm_agent/eval/test_set.json:1-4`
- `scm_agent/eval/eval_harness.py:623-685`
- `scm_agent/eval/eval_harness.py:927-943`
- `scm_agent/eval/eval_harness.py:1297-1304`

Confirmed:

- `schema_file` is declared but ignored; main always loads the default schema.
- Unknown dialects, missing/generated IDs, empty difficulties, and duplicate tags
  are accepted.
- Duplicate tags can double-count a case in tag breakdowns.
- Results omit test-set/schema hashes, Git commit, exact model revision, adapter
  checkpoint/hash, and train/test overlap evidence.
- Reference and candidate queries do not share one immutable database snapshot.

Required changes: strict versioned JSON validation, honor/hash `schema_file`,
immutable evaluation snapshots, full run provenance, and automated training/test
overlap checks.

## Moderate findings

### M1 — Pillar 1 has embedded self-tests, not a discoverable regression suite

The seven query/API/evaluation modules put assertions under
`if __name__ == "__main__"`. They are useful smoke tests, but unittest/pytest
discovery only covers the healing subsystem. There is no CI configuration.

Move the checks into external test modules and run them in CI. Small executable
demos can remain, but they must not be the only regression coverage.

### M2 — Multi-dialect/live claims must remain limited

PostgreSQL, MySQL, and SQL Server introspection have not run on real servers;
MySQL and SQL Server are not wired into `open_connection`; Psycopg and the OpenAI
SDK are absent in the reviewed environment; and all five live PostgreSQL healing
tests are skipped behind explicit guards.

Report these as implemented but unverified adapters, not working multi-dialect
runtime support.

### M3 — Evaluation labels/business meaning need correction

- Four cases tagged `order-by` neither request nor use ordering.
- The real ranked cases use `order-by-limit`, creating a misleading tag report.
- `medium-03` treats a cancelled order as revenue while asking for money made.
- Stage 1 is a canned keyword matcher; 7.1% is a smoke result, not a model
  baseline.

### M4 — Repository hygiene

The repository tracks a partial `.venv`, machine-specific `pyvenv.cfg`, and 41
`__pycache__`/`.pyc` files. It has no `.gitignore`, no dependency lock, and very
large production modules containing embedded test suites.

Add `.gitignore`, remove generated artifacts from tracking, add a reproducible
constraints/lock strategy, split tests from production modules, and archive design
history rather than deleting the current copies.

### M5 — Evaluation edge cases

- `--max-rows` accepts zero/negative values; SQLite `fetchmany(0 or negative)` may
  read all rows.
- The evaluator's order lexer does not mask PostgreSQL dollar-quoted text.
- Non-finite numeric values canonicalize inconsistently across numeric types.

## What is good and should be preserved

- Clear separation of configuration, schema discovery, model access, extraction,
  safety, execution, API shaping, and evaluation.
- The safety gate executes the normalized checked text, not the raw model output.
- Strong handling of stacked statements, data-modifying CTEs, `SELECT INTO`,
  comments, literals, and PostgreSQL dollar quoting.
- Lazy optional imports keep standard-library smoke paths usable.
- Structured results prevent most request-path crashes.
- Thoughtful numeric normalization, multiset comparison, and order-sensitive
  comparison foundations.
- Internally consistent synthetic schema with deliberate LEFT-vs-INNER join cases.
- Strong healing design: observe/dry-run/act separation, typed identities, atomic
  intent and cooldown, before/after snapshots, final revalidation, orphan recovery,
  action limits, and guarded demos.

## Claude's later healing changes

No new critical/important regression was found in the later healing edits:

- `audit.py`: the 15-minute failed-VACUUM backoff reduces rapid retry storms and
  remains inside the atomic intent transaction.
- `demo.py`: `mod(id, 4)` fixes Psycopg percent handling; effective-database checks
  are stricter even when an override is present.
- `models.py`: empty PostgreSQL `application_name` is valid and is now accepted
  while NUL/type checks remain.

Preserve these fixes. The outstanding limitation is that live PostgreSQL behavior
has still not been executed in this environment.

## Fresh verification evidence

```text
python3 -m scm_agent.pipeline.config             PASS
python3 -m scm_agent.pipeline.schema_context     PASS
python3 -m scm_agent.pipeline.safety             PASS (42/42)
python3 -m scm_agent.pipeline.model_client       PASS
python3 -m scm_agent.pipeline.nl_to_sql           PASS
python3 -m scm_agent.app.main                    PASS (81/81)
python3 -m scm_agent.eval.eval_harness            process exit 0
                                                   mock 1/14 = 7.1%
                                                   1 reference skipped
healing unittest suite                            187 total
                                                   182 pass, 5 guarded live skips
repository-wide unittest discovery                187 total
                                                   182 pass, 5 guarded live skips
python3 -m compileall -q scm_agent                PASS
git diff --check                                  PASS
```

Dependency availability during review:

```text
fastapi=True, uvicorn=True
psycopg=False, openai=False, pymysql=False, pyodbc=False
```

Therefore native PostgreSQL query execution, real OpenAI-compatible model calls,
and MySQL/SQL Server behavior were not verified.

## Required repair order

1. Disable samples by default and add data-leak regressions.
2. Make truncated evaluation unscored/inconclusive.
3. Eliminate model/database secret exposure.
4. Make PostgreSQL read-only setup fail closed and use a least-privilege role.
5. Remove the unsafe port-5432 default.
6. Add model/database/request timeouts and resource budgets.
7. Correct evaluation health accounting and preflight references.
8. Lock evaluation to an immutable compatible fixture and record provenance.
9. Add authentication/authorization or enforce localhost-only demo scope.
10. Add discoverable Pillar 1 tests, CI, and repository cleanup.

After items 1-8, rerun all tests and a disposable live PostgreSQL matrix before
generating stage-2 and stage-3 numbers for the FYP report.

