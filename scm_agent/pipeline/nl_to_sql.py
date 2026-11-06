"""Orchestration — an English question in, real rows out.

This is the module that joins the four pieces that already exist into one
pipeline, and it is the only place that knows the *order* they run in:

    question
      -> schema_context.introspect()      what tables exist (BO-6, plug-and-play)
      -> build_prompt()                   the OmniSQL prompt contract
      -> model_client.generate()          mock / base OmniSQL / fine-tuned LoRA
      -> extract_sql()                    reasoning prose -> just the SQL
      -> safety.check_sql()               read-only enforcement (BO-7)
      -> execute                          only if the verdict allowed it
      -> NLToSQLResult                    one record the API and eval both use

That order is security-critical, not stylistic. The safety check sits between
the model and the database, and *nothing* reaches `cursor.execute()` without
passing it. Just as important, we execute `verdict.normalized_sql` rather than
the string we extracted: `safety.py` inspects the text after stripping comments
and trailing semicolons, so running anything else would mean checking one
string and executing a different one — the classic way a safety layer gets
quietly bypassed. Its docstring on `normalized_sql` and `assert_safe` spells
this out; this module is the caller that has to honour it.

Two contracts this file must not break
--------------------------------------
1.  `OMNISQL_PROMPT_TEMPLATE` below is reproduced verbatim from the OmniSQL
    model card, with one deliberate change: the stock template hardcodes
    `SQLite` as the engine and we substitute `DialectSpec.prompt_name` instead,
    which is how the project supports four dialects (docs/PROJECT_BRIEF.md).
    Teammates are generating fine-tuning data against this exact wording. If
    the training format and the inference format drift apart, the fine-tuned
    model quietly gets worse and nothing in the code looks broken — this is
    "train/serve skew", and docs/PROJECT_BRIEF.md forbids changing the prompt
    unilaterally once data generation has started.

2.  `answer_question()` never raises. It sits on a web request path, so every
    failure — a dead database, an unparseable model reply, a refused query —
    comes back as an `NLToSQLResult` with `.error` set. A 500 traceback is not
    an acceptable answer to a user, and it is a terrible one during a live
    viva demo.

Connections are plain DB-API 2.0 objects (`sqlite3.Connection`,
`psycopg.Connection`, ...), so this module imports no database driver at all.
"""

from __future__ import annotations

import re
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from scm_agent.pipeline.config import (
    DIALECT_SPECS,
    AppConfig,
    Dialect,
    get_config,
)
from scm_agent.pipeline.model_client import (
    FENCE_CLOSE,
    SQL_FENCE_OPEN,
    ModelClient,
    get_model_client,
)
from scm_agent.pipeline.safety import SafetyVerdict, check_sql
from scm_agent.pipeline.schema_context import DatabaseSchema, introspect

# --------------------------------------------------------------------------
# 1. The prompt — a shared contract, kept as one greppable constant
# --------------------------------------------------------------------------

OMNISQL_PROMPT_TEMPLATE = """Task Overview:
You are a data science expert. Below, you are provided with a database schema and a natural language question. Your task is to understand the schema and generate a valid SQL query to answer the question.

Database Engine:
{db_engine}

Database Schema:
{db_details}
This schema describes the database's structure, including tables, columns, primary keys, foreign keys, and any relevant relationships or constraints.

Question:
{question}

Instructions:
- Make sure you only output the information that is asked in the question. If the question asks for a specific column, make sure to only include that column in the SELECT clause, nothing more.
- The generated query should return all of the information asked in the question without any missing or extra information.
- Before generating the final SQL query, please think through the steps of how to write the query.

Output Format:
In your answer, please enclose the generated SQL query in a code block:
```sql
-- Your SQL query
```

Take a deep breath and think step by step to find the correct SQL query."""
"""OmniSQL's published prompt, verbatim except for `{db_engine}`.

Deliberately a module-level constant rather than an f-string buried in a
function: it is the interface between the fine-tuning data Person A generates
and the inference path, so it has to be findable with one `grep` and reviewable
as a single block of text.

The only three braces in the template are the three placeholders, which is what
makes `str.format()` safe here — values substituted *into* a format string are
never re-scanned for braces, so a schema or a question containing `{` cannot
break it. If anyone ever adds a literal brace to the template itself, this
switches to `.replace()`.
"""

PROMPT_PLACEHOLDERS: tuple[str, ...] = ("{db_engine}", "{db_details}", "{question}")
"""What `build_prompt` must fill. Asserted in the self-test so an accidental
edit to the template above fails loudly instead of silently."""


def build_prompt(question: str, ddl: str, dialect: Dialect) -> str:
    """Fill the OmniSQL template for one question against one schema.

    `ddl` is `DatabaseSchema.to_ddl()` output — `CREATE TABLE` statements, with
    sample values as trailing comments when they were collected.

    The engine name comes from `DIALECT_SPECS[dialect].prompt_name`, never from
    a literal here. docs/ARCHITECTURE.md: all dialect facts live in
    `config.py`, so an unregistered dialect raises `KeyError` right here rather
    than producing a prompt that quietly claims the wrong engine.
    """
    spec = DIALECT_SPECS[dialect]
    return OMNISQL_PROMPT_TEMPLATE.format(
        db_engine=spec.prompt_name,
        db_details=ddl.strip(),
        question=question.strip(),
    )


# --------------------------------------------------------------------------
# 2. Pulling the SQL back out of a reasoning-shaped reply
# --------------------------------------------------------------------------

class SQLExtractionError(ValueError):
    """The model's reply contained nothing that looks like a SQL query.

    Its own class so the API layer can distinguish "the model misbehaved" from
    "the query was refused" from "the database is down" — three different
    problems that need three different messages on screen.
    """


# The mock and the real model both emit ```` ```sql ... ``` ````. Deriving the
# language tag from the two exported constants means the parser cannot drift
# away from the generator: if `SQL_FENCE_OPEN` ever changes, this breaks here,
# at import time, instead of silently failing to match at runtime.
assert SQL_FENCE_OPEN.startswith(FENCE_CLOSE), "fence constants disagree"
_FENCE_LANGUAGE = SQL_FENCE_OPEN[len(FENCE_CLOSE):]  # "sql"
_LANGUAGE_TAG = r"[A-Za-z0-9_+#-]*"
assert re.fullmatch(_LANGUAGE_TAG, _FENCE_LANGUAGE), (
    f"the project's own fence tag {_FENCE_LANGUAGE!r} would not be recognised "
    "by this parser"
)

_FENCE_BLOCK = re.compile(
    re.escape(FENCE_CLOSE)                    # opening ```
    + rf"[ \t]*{_LANGUAGE_TAG}[ \t]*\r?\n"    # optional language tag, then EOL
    + r"(.*?)"                                # the body, non-greedy
    + re.escape(FENCE_CLOSE),                 # closing ```
    re.DOTALL,
)
"""Matches ```` ```sql ... ``` ```` **and** a bare ```` ``` ... ``` ````.

The language tag is optional and unvalidated — some models tag the block
`postgresql`, some tag nothing at all, and rejecting those would be a parser
bug dressed up as strictness.
"""

_PLACEHOLDER_COMMENT = re.compile(r"^\s*--\s*Your SQL query\s*$", re.IGNORECASE)
"""The literal comment inside the template's example block.

Smaller models sometimes copy the example verbatim before writing the real
query. Left in place it is harmless (the safety layer strips comments), but it
makes the logged SQL confusing to read, so it goes."""

_SQL_START = re.compile(r"(?<![A-Za-z0-9_$])(SELECT|WITH)(?![A-Za-z0-9_$])", re.I)
"""Last-resort anchor when the model forgets the fence entirely.

Word-boundary-aware for the same reason `safety.py` spells its alphabet out:
`reselect_flag` must not look like the start of a statement."""


def extract_sql(raw: str) -> str:
    """Return just the SQL from a model reply, or raise `SQLExtractionError`.

    A real OmniSQL answer is *reasoning prose first, then a fenced block* — the
    template above literally instructs it to think out loud before answering.
    So the naive `raw.strip()` a first draft would use is wrong on every single
    real response. The shapes handled here, in order:

    1. ```` ```sql ... ``` ```` — the documented output contract.
    2. ```` ``` ... ``` ```` — no language tag; common from smaller models.
    3. Several fenced blocks — a draft, then a corrected final version. We take
       the **last** one, because that is the model's own final answer.
    4. Prose after the closing fence ("This query works because...") — ignored,
       since the regex stops at the fence.
    5. No fence at all — fall back to scanning for the first `SELECT`/`WITH`.

    Nothing here is a security control. Extraction only decides *what* gets
    checked; `check_sql` decides what runs. A wrong extraction produces a
    syntax error or a denial, never an unchecked query.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise SQLExtractionError("the model returned an empty response")

    # Shapes 1-4: take the last fenced block that actually contains something.
    # Scanning from the end and skipping empties handles the real-world case of
    # a trailing empty fence left behind by a truncated generation.
    for body in reversed(_FENCE_BLOCK.findall(raw)):
        candidate = _clean_sql_body(body)
        if candidate:
            return candidate

    # Shape 5: no usable fence. Anchor on the first read keyword and take the
    # rest, cut at the first statement-ending semicolon.
    match = _SQL_START.search(raw)
    if match is not None:
        candidate = _clean_sql_body(_cut_at_statement_end(raw[match.start():]))
        if candidate:
            return candidate

    preview = " ".join(raw.split())[:160]
    raise SQLExtractionError(
        "no SQL found in the model response — expected a "
        f"{SQL_FENCE_OPEN} block or a statement starting with SELECT/WITH. "
        f"Response began: {preview!r}"
    )


def _clean_sql_body(body: str) -> str:
    """Tidy an extracted block: drop the placeholder comment, trim, unfence."""
    lines = body.splitlines()
    # Only *leading* placeholder lines are dropped. A `--` comment further down
    # could be the model annotating a real clause, and rewriting the middle of a
    # query we are about to run is not something this function should do.
    while lines and (not lines[0].strip() or _PLACEHOLDER_COMMENT.match(lines[0])):
        lines.pop(0)
    text = "\n".join(lines).strip()
    # A stray fence can survive the fallback path (e.g. a single-line
    # ```` ```SELECT 1``` ````).
    #
    # This must strip only a COMPLETE fence marker. `str.strip(FENCE_CLOSE)`
    # would strip the *character set* {`}, which silently eats the closing
    # backtick of a MySQL quoted identifier:
    #     SELECT full_name FROM `customers`  ->  SELECT full_name FROM `customers
    # producing "unterminated quoted identifier" on a perfectly valid query.
    # That bug was invisible to safety.py's unit tests because they call
    # check_sql() directly and never pass through extraction.
    while text.startswith(FENCE_CLOSE):
        text = text[len(FENCE_CLOSE):].strip()
    while text.endswith(FENCE_CLOSE):
        text = text[: -len(FENCE_CLOSE)].strip()
    return text


# Statement-initial keywords. If one of these follows the first semicolon, the
# reply holds more than one statement and must be handed to check_sql WHOLE so
# the stacked-statement rule actually fires.
_NEXT_STATEMENT = re.compile(
    r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT"
    r"|REVOKE|MERGE|REPLACE|CALL|EXEC(?:UTE)?|COPY|VACUUM|REINDEX|ATTACH"
    r"|DETACH|PRAGMA|SET|BEGIN|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)


def _cut_at_statement_end(text: str) -> str:
    """Truncate at the first semicolon that really ends the statement.

    Only used on the unfenced fallback path, where prose may follow the query
    with no delimiter to stop at.

    Two things this must get right, both found by adversarial review:

    1. **Comments.** A semicolon inside `-- ...` or a block comment does not end
       the statement. Cutting there silently rewrites a valid query:
           SELECT name FROM products -- prices in USD; tax excluded
           WHERE in_stock = 0
       used to become `SELECT name FROM products -- prices in USD;`, whose
       comment safety.py then strips — executing an unfiltered
       `SELECT name FROM products` and returning 11 rows instead of 1, reported
       to the user as a success. Shrinking the string is safe; changing its
       meaning is not.

    2. **Stacked statements.** If another statement follows the semicolon, the
       whole reply is returned unchanged so `check_sql` denies it for the right
       reason. Cutting first would leave an `allowed=True` audit record for what
       was actually an injection attempt, which breaks the BO-8 audit trail even
       though the payload never ran.
    """
    index = 0
    length = len(text)
    while index < length:
        char = text[index]

        if char == "'":  # string literal, doubled quote escapes
            index += 1
            while index < length:
                if text[index] == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        index += 2
                        continue
                    break
                index += 1
            index += 1
            continue

        if text.startswith("--", index):  # line comment, runs to end of line
            newline = text.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue

        if text.startswith("/*", index):  # block comment
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue

        if char == ";":
            remainder = text[index + 1:]
            if _NEXT_STATEMENT.match(remainder):
                return text  # let check_sql see, and deny, the whole thing
            return text[: index + 1]

        index += 1

    return text


# --------------------------------------------------------------------------
# 3. The result record — one object, consumed by both the API and the eval
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class NLToSQLResult:
    """Everything that happened for one question, success or failure.

    Frozen for the same reason `SafetyVerdict` is: this is an audit record
    (BO-8), and a record that can be edited after the fact proves nothing.

    Deliberately fat. `app/main.py` needs the rows, `eval/eval_harness.py`
    needs the SQL to compare, the audit view needs the verdict, and error
    analysis after the fine-tune needs the prompt and the raw reply that
    produced a bad query. Splitting these into three types would just mean
    three lookups later.
    """

    question: str
    prompt: str
    """The exact text sent to the model. Kept so a wrong answer can be
    reproduced offline without a database."""

    raw_response: str
    """The model's complete, unparsed reply, including its reasoning prose."""

    sql: str | None
    """What `extract_sql` pulled out — None if extraction failed."""

    executed_sql: str | None
    """`SafetyVerdict.normalized_sql`, i.e. the text actually handed to the
    driver. Differs from `sql` by having comments and trailing semicolons
    removed. None when nothing was executed."""

    safety: SafetyVerdict | None
    """The read-only verdict. None only if we never got as far as checking."""

    columns: tuple[str, ...] = ()
    rows: tuple[tuple[object, ...], ...] = ()
    truncated: bool = False
    """True when the query returned more rows than `max_rows` and we stopped."""

    error: str | None = None
    """Human-readable failure reason, including safety denials. `None` means the
    query ran."""

    stage: str = "unknown"
    """`ModelConfig.stage` — "stage-1-mock" / "stage-2-base" /
    "stage-3-finetuned". Recorded per query so a results table can never be
    ambiguous about which model produced a number."""

    dialect: str = Dialect.POSTGRESQL.value
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """True only if a safe query ran and produced a result set."""
        return self.error is None and self.safety is not None and self.safety.allowed

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dict(self, include_prompt: bool = True) -> dict[str, object]:
        """JSON-serialisable view, for the API response and the audit log.

        Row values are coerced here rather than at the call site because real
        drivers hand back `Decimal`, `date` and `memoryview` objects, none of
        which `json.dumps` can encode. Doing it once, here, means neither
        FastAPI nor the eval harness has to own a custom encoder.

        `include_prompt=False` drops the two big fields — a prompt with a large
        schema is easily 10 kB, which is worth logging once but not worth
        shipping to a browser on every keystroke.
        """
        payload: dict[str, object] = {
            "ok": self.ok,
            "question": self.question,
            "sql": self.sql,
            "executed_sql": self.executed_sql,
            "safety": self.safety.to_log() if self.safety is not None else None,
            "columns": list(self.columns),
            "rows": [[_jsonable(value) for value in row] for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "error": self.error,
            "stage": self.stage,
            "dialect": self.dialect,
            "duration_ms": round(self.duration_ms, 2),
        }
        if include_prompt:
            payload["prompt"] = self.prompt
            payload["raw_response"] = self.raw_response
        return payload


def _jsonable(value: object) -> object:
    """Coerce one database value into something `json.dumps` accepts."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        # NUMERIC(10,2) arrives as Decimal from psycopg. float loses precision
        # in the last bits, but this is a display/comparison payload, not money
        # arithmetic — and JSON has no decimal type to preserve it in anyway.
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


# --------------------------------------------------------------------------
# 4. Schema DDL cache
# --------------------------------------------------------------------------

SCHEMA_CACHE_MAX_ENTRIES: int = 8
"""How many (connection, schema) pairs to remember. Small on purpose — see
`_SCHEMA_CACHE` for why an entry is not free."""

_SCHEMA_CACHE: dict[tuple, tuple[object, str, DatabaseSchema]] = {}
"""Keyed by connection identity; the value keeps a reference to the connection.

Introspecting on every question means a dozen catalog queries per keystroke,
which is wasteful on the demo database and rude on a customer's. But caching by
`id(conn)` alone would be a real bug: CPython reuses the id of a garbage
collected object, so a closed connection's id could later belong to a
*different* connection and serve it someone else's schema. Storing the
connection object in the entry keeps it alive, which makes the id stable for as
long as we rely on it. That is a deliberate, bounded leak — hence the small cap
and `clear_schema_cache()`.

Not thread-safe, and it does not need to be: a dict assignment is atomic under
the GIL, so the worst concurrent outcome is introspecting twice.
"""


def get_schema_ddl(
    conn,
    dialect: Dialect,
    schema: str | None = None,
    include_samples: bool = False,
    refresh: bool = False,
) -> tuple[str, DatabaseSchema]:
    """Return `(ddl, schema_object)` for `conn`, cached.

    `refresh=True` bypasses and repopulates the cache — the escape hatch for
    "someone just added a table and the agent cannot see it".
    """
    key = (id(conn), dialect.value, schema, include_samples)
    if not refresh:
        cached = _SCHEMA_CACHE.get(key)
        if cached is not None:
            _, ddl, database = cached
            return ddl, database

    database, samples = introspect(
        conn, dialect, schema=schema, include_samples=include_samples
    )
    ddl = database.to_ddl(samples)

    if len(_SCHEMA_CACHE) >= SCHEMA_CACHE_MAX_ENTRIES and key not in _SCHEMA_CACHE:
        # Plain FIFO eviction. An LRU would be better and is not worth the
        # bookkeeping for a cache that holds single digits of entries.
        _SCHEMA_CACHE.pop(next(iter(_SCHEMA_CACHE)))
    _SCHEMA_CACHE[key] = (conn, ddl, database)
    return ddl, database


def clear_schema_cache(conn=None) -> int:
    """Forget cached DDL — for one connection, or all of it. Returns the count.

    Call this when closing a connection: it releases the reference the cache
    holds, which is the only thing keeping the object alive.
    """
    if conn is None:
        count = len(_SCHEMA_CACHE)
        _SCHEMA_CACHE.clear()
        return count
    doomed = [key for key in _SCHEMA_CACHE if key[0] == id(conn)]
    for key in doomed:
        _SCHEMA_CACHE.pop(key, None)
    return len(doomed)


# --------------------------------------------------------------------------
# 5. The pipeline
# --------------------------------------------------------------------------

DEFAULT_MAX_ROWS: int = 200
"""Row ceiling for one answer. A question like "list every order" should not be
able to pull a million rows through a web response — and nobody reads past the
first screen anyway. `NLToSQLResult.truncated` tells the caller it happened."""


def answer_question(
    question: str,
    conn,
    config: AppConfig | None = None,
    include_samples: bool = False,
    schema: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    refresh_schema: bool = False,
    client: ModelClient | None = None,
) -> NLToSQLResult:
    """Run the whole pipeline for one question. Never raises.

    Arguments:
        question:       the user's English question.
        conn:           an open DB-API 2.0 connection to the target database.
        config:         defaults to `get_config()`, i.e. the environment. Tests
                        and the eval harness pass one explicitly so they never
                        depend on the developer's shell.
        include_samples: embed a few real column values in the prompt as
                        comments. Costs one query per column, and materially
                        improves filter accuracy ('delivered' vs 'DELIVERED').
                        Turn it off on a database whose values must not appear
                        in a prompt.
        max_rows:       result-set ceiling, see `DEFAULT_MAX_ROWS`.
        refresh_schema: bypass the DDL cache for this call.
        client:         reuse a `ModelClient` instead of building one. The eval
                        harness runs hundreds of questions and should not open a
                        new HTTP client for each.
    """
    started = _time.perf_counter()
    if config is None:
        config = get_config()
    dialect = config.database.dialect
    stage = config.model.stage

    def failed(
        message: str,
        prompt: str = "",
        raw: str = "",
        sql: str | None = None,
        verdict: SafetyVerdict | None = None,
    ) -> NLToSQLResult:
        return NLToSQLResult(
            question=question,
            prompt=prompt,
            raw_response=raw,
            sql=sql,
            executed_sql=None,
            safety=verdict,
            error=message,
            stage=stage,
            dialect=dialect.value,
            duration_ms=(_time.perf_counter() - started) * 1000,
        )

    if not question or not question.strip():
        return failed("empty question")

    # --- step 1: what does this database look like? ------------------------
    try:
        ddl, _database = get_schema_ddl(
            conn,
            dialect,
            schema=schema,
            include_samples=include_samples,
            refresh=refresh_schema,
        )
    except Exception as exc:
        return failed(f"schema introspection failed: {type(exc).__name__}: {exc}")

    if not ddl.strip():
        return failed(
            "the database has no tables to query (introspection returned an "
            "empty schema) — check the connection and the schema name"
        )

    # --- step 2: build the prompt ------------------------------------------
    try:
        prompt = build_prompt(question, ddl, dialect)
    except Exception as exc:
        return failed(f"prompt construction failed: {type(exc).__name__}: {exc}")

    # --- step 3: ask the model ---------------------------------------------
    try:
        if client is None:
            client = get_model_client(config.model)
        raw = client.generate(prompt)
    except Exception as exc:
        # Covers ModelClientError (server unreachable, package missing) and any
        # SDK exception underneath it. The message is already actionable.
        return failed(f"model call failed: {exc}", prompt=prompt)

    return _check_and_execute(
        question=question,
        prompt=prompt,
        raw=raw,
        conn=conn,
        dialect=dialect,
        stage=stage,
        max_rows=max_rows,
        started=started,
    )


def _check_and_execute(
    question: str,
    prompt: str,
    raw: str,
    conn,
    dialect: Dialect,
    stage: str,
    max_rows: int,
    started: float,
) -> NLToSQLResult:
    """Steps 4-6: extract -> safety check -> execute.

    Split out from `answer_question` so it can be driven with a *forged* model
    reply. That is not a convenience — it is the only honest way to test that a
    `DROP TABLE` is blocked, because the mock model will never produce one and a
    test that cannot produce the dangerous input is not testing the guard.
    """

    def result(**overrides) -> NLToSQLResult:
        base = {
            "question": question,
            "prompt": prompt,
            "raw_response": raw,
            "sql": None,
            "executed_sql": None,
            "safety": None,
            "stage": stage,
            "dialect": dialect.value,
            "duration_ms": (_time.perf_counter() - started) * 1000,
        }
        base.update(overrides)
        return NLToSQLResult(**base)

    # --- step 4: parse the reply -------------------------------------------
    try:
        sql = extract_sql(raw)
    except SQLExtractionError as exc:
        return result(error=str(exc))
    except Exception as exc:  # a parser bug must not 500 the request
        return result(error=f"SQL extraction failed: {type(exc).__name__}: {exc}")

    # --- step 5: the gate ---------------------------------------------------
    # `check_sql` is documented never to raise, but it is wrapped anyway: an
    # exception escaping the *safety layer* is the one failure mode where
    # falling through to execution would be catastrophic.
    try:
        verdict = check_sql(sql, dialect)
    except Exception as exc:
        return result(sql=sql, error=f"safety check errored, refusing: {exc}")

    if not verdict.allowed:
        # Denied. Nothing below this line touches the database.
        return result(
            sql=sql,
            safety=verdict,
            error=(
                "query blocked by the read-only safety layer: "
                f"{verdict.reason}. This path only executes SELECT / "
                "WITH ... SELECT statements."
            ),
        )

    # --- step 6: execute the text the safety layer inspected ---------------
    runnable = verdict.normalized_sql
    try:
        columns, rows, truncated = _execute(conn, runnable, max_rows)
    except Exception as exc:
        return result(
            sql=sql,
            executed_sql=runnable,
            safety=verdict,
            error=f"query execution failed: {type(exc).__name__}: {exc}",
        )

    return result(
        sql=sql,
        executed_sql=runnable,
        safety=verdict,
        columns=columns,
        rows=rows,
        truncated=truncated,
    )


def _execute(
    conn, sql: str, max_rows: int
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...], bool]:
    """Run a checked read and return `(columns, rows, truncated)`.

    Fetches one row more than the ceiling so "there were more" is a fact rather
    than a guess, then discards it.

    The `rollback()` at the end is not paranoia about writes — there are none by
    construction. It ends the implicit transaction PostgreSQL opens for a plain
    `SELECT`. Left open, that connection sits `idle in transaction`, which is
    precisely the anomaly the self-healing engine (Pillar 2) is built to detect
    and kill. Making Pillar 1 generate the alerts Pillar 2 fires on would be an
    unfortunate thing to discover during the demo.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        if cursor.description is None:
            # A statement with no result set. The safety layer should have made
            # this impossible, so treat it as an empty read rather than crashing.
            return (), (), False
        columns = tuple(str(d[0]) for d in cursor.description)
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = tuple(tuple(row) for row in fetched[:max_rows])
        return columns, rows, truncated
    finally:
        for close in (cursor.close, getattr(conn, "rollback", lambda: None)):
            try:
                close()
            except Exception:
                pass  # best effort; a cleanup failure must not mask the result


# --------------------------------------------------------------------------
# Self-test — the whole pipeline, end to end, on the mock provider
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    import os
    import sqlite3
    from pathlib import Path

    RULE = "=" * 78

    def build_test_db() -> sqlite3.Connection:
        """Load `db/schema.sql` into an in-memory SQLite database.

        The file is written for PostgreSQL, so two translations are needed:
        `SERIAL PRIMARY KEY` -> `INTEGER PRIMARY KEY` (SQLite's rowid alias,
        which auto-increments the same way), and `CREATE INDEX` dropped (valid
        in SQLite, but irrelevant to correctness and noise in this test).

        Using the real schema file rather than a hand-written copy is the point:
        if a teammate adds a table to `schema.sql`, this test starts exercising
        it immediately instead of drifting quietly out of date.
        """
        path = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
        script = path.read_text(encoding="utf-8")
        script = re.sub(
            r"(?i)\bSERIAL\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY", script
        )
        script = re.sub(r"(?is)\bCREATE\s+INDEX\b.*?;", "", script)
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(script)
        connection.commit()
        return connection

    def show_rows(res: NLToSQLResult, limit: int = 4) -> None:
        if not res.columns:
            print("      (no result set)")
            return
        print("      " + " | ".join(res.columns))
        for row in res.rows[:limit]:
            print("      " + " | ".join(str(_jsonable(v)) for v in row))
        if res.row_count > limit:
            print(f"      ... and {res.row_count - limit} more row(s)")

    # Stage 1 + SQLite target, set through the environment so the swap point in
    # config.py is the thing being exercised, not bypassed.
    os.environ["SCM_MODEL_PROVIDER"] = "mock"
    os.environ["SCM_DIALECT"] = "sqlite"
    os.environ["SCM_DB_DSN"] = "sqlite:///:memory:"
    cfg = get_config()

    conn = build_test_db()
    print(RULE)
    print("SETUP")
    print(RULE)
    print(f"  stage          : {cfg.model.stage}")
    print(f"  dialect        : {cfg.database.dialect.value} "
          f"(prompt engine = {cfg.database.spec.prompt_name!r})")
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    print(f"  tables loaded  : {tables}")

    QUESTIONS = [
        "How many customers are there?",
        "List all products by price",
        "What is the total revenue?",
        "How many orders does each customer have?",
        "Which product sells the most?",
        "Tell me something interesting about the database",
    ]

    # ------------------------------------------------------------------
    # The prompt, in full, exactly once — so a human can eyeball the contract.
    # ------------------------------------------------------------------
    ddl_text, db_schema = get_schema_ddl(conn, cfg.database.dialect)
    print()
    print(RULE)
    print("THE PROMPT SENT TO THE MODEL  (verbatim OmniSQL template)")
    print(RULE)
    print(build_prompt(QUESTIONS[0], ddl_text, cfg.database.dialect))
    print(RULE)

    # ------------------------------------------------------------------
    # Full pipeline, every question.
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("FULL PIPELINE  (question -> prompt -> model -> SQL -> safety -> rows)")
    print(RULE)
    results: list[NLToSQLResult] = []
    for index, q in enumerate(QUESTIONS, start=1):
        res = answer_question(q, conn, config=cfg)
        results.append(res)
        print(f"\n[{index}] {q}")
        print("    SQL:")
        for line in (res.sql or "<none extracted>").splitlines():
            print(f"      {line}")
        if res.safety is not None:
            print(f"    safety : allowed={res.safety.allowed}  "
                  f"reason={res.safety.reason!r}")
        else:
            print("    safety : not reached")
        print(f"    error  : {res.error}")
        print(f"    rows   : {res.row_count}"
              f"{' (truncated)' if res.truncated else ''}")
        show_rows(res)

    # ------------------------------------------------------------------
    # (a) at least 5 questions return rows with no error
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("CHECK (a) — end-to-end success")
    print(RULE)
    good = [r for r in results if r.ok and r.row_count > 0]
    for r in good:
        print(f"  ok  rows={r.row_count:<3} {r.question}")
    print(f"  {len(good)}/{len(results)} questions returned rows with no error")
    assert len(good) >= 5, f"expected >=5 clean answers, got {len(good)}"

    for r in results:
        assert r.stage == "stage-1-mock", r.stage
        assert r.dialect == "sqlite", r.dialect
        assert r.executed_sql is not None and ";" not in r.executed_sql, r.executed_sql
    print("  every result carries the stage and dialect it was produced under")
    print("  executed SQL is the safety layer's normalized_sql (no semicolons)")

    payload = _json.dumps(results[0].to_dict())
    slim = results[1].to_dict(include_prompt=False)
    print(f"  to_dict() is JSON-serialisable            : {len(payload)} bytes")
    print(f"  to_dict(include_prompt=False) drops prompt: {'prompt' not in slim}")
    assert "prompt" not in slim and "raw_response" not in slim

    # ------------------------------------------------------------------
    # (b) a write is blocked BEFORE execution
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("CHECK (b) — a hostile model reply never reaches the database")
    print(RULE)

    FORGERIES = [
        (
            "plain DROP",
            "The user asked to tidy up, so I will remove the table.\n\n"
            f"{SQL_FENCE_OPEN}\nDROP TABLE orders;\n{FENCE_CLOSE}",
        ),
        (
            "stacked statement smuggled after a SELECT",
            "First I read the table, then I clean it.\n\n"
            f"{SQL_FENCE_OPEN}\nSELECT * FROM customers; DROP TABLE customers;\n"
            f"{FENCE_CLOSE}",
        ),
        (
            "data-modifying CTE (starts with WITH, still writes)",
            "A CTE lets me do both at once.\n\n"
            f"{SQL_FENCE_OPEN}\nWITH gone AS (DELETE FROM order_items RETURNING *)\n"
            f"SELECT COUNT(*) FROM gone;\n{FENCE_CLOSE}",
        ),
        (
            "payload hidden behind a line comment",
            f"Here you go.\n\n{SQL_FENCE_OPEN}\nSELECT 1 --\nDROP TABLE products;\n"
            f"{FENCE_CLOSE}",
        ),
    ]

    def table_counts() -> dict[str, int]:
        return {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in tables
        }

    before = table_counts()
    print(f"  row counts before : {before}")

    for name, forged in FORGERIES:
        blocked = _check_and_execute(
            question="please clean up the database",
            prompt="<forged>",
            raw=forged,
            conn=conn,
            dialect=cfg.database.dialect,
            stage=cfg.model.stage,
            max_rows=DEFAULT_MAX_ROWS,
            started=_time.perf_counter(),
        )
        print(f"\n  {name}")
        print(f"    extracted   : {(blocked.sql or '').splitlines()[0]!r} ...")
        print(f"    allowed     : {blocked.safety.allowed}")
        print(f"    violations  : {list(blocked.safety.violations)}")
        print(f"    executed_sql: {blocked.executed_sql}")
        print(f"    error       : {blocked.error}")
        assert blocked.safety is not None and blocked.safety.allowed is False
        assert blocked.executed_sql is None, "denied SQL must never be executed"
        assert blocked.rows == () and blocked.columns == ()
        assert blocked.ok is False
        assert "blocked by the read-only safety layer" in (blocked.error or "")

    after = table_counts()
    print(f"\n  row counts after  : {after}")
    assert after == before, "the database changed — a write got through!"
    assert set(tables) == {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }, "a table disappeared"
    print("  every table still present, every row count unchanged")

    # ------------------------------------------------------------------
    # (c) extract_sql handles every parsing shape
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("CHECK (c) — extract_sql against every reply shape")
    print(RULE)

    SHAPES: list[tuple[str, str, str]] = [
        (
            "1. ```sql fence after reasoning prose",
            "I need to count the rows in customers.\n\n"
            f"{SQL_FENCE_OPEN}\nSELECT COUNT(*) FROM customers;\n{FENCE_CLOSE}",
            "SELECT COUNT(*) FROM customers;",
        ),
        (
            "2. bare ``` fence, no language tag",
            "Straightforward single-table read.\n\n"
            f"{FENCE_CLOSE}\nSELECT name FROM products;\n{FENCE_CLOSE}",
            "SELECT name FROM products;",
        ),
        (
            "3. two blocks — the LAST one wins (draft, then correction)",
            "First attempt:\n\n"
            f"{SQL_FENCE_OPEN}\nSELECT * FROM order;\n{FENCE_CLOSE}\n\n"
            "That table name is wrong, the table is plural:\n\n"
            f"{SQL_FENCE_OPEN}\nSELECT * FROM orders;\n{FENCE_CLOSE}",
            "SELECT * FROM orders;",
        ),
        (
            "4. trailing prose after the closing fence",
            f"Here is the query.\n\n{SQL_FENCE_OPEN}\nSELECT city FROM customers;\n"
            f"{FENCE_CLOSE}\n\nThis returns one row per customer, including "
            "duplicates.",
            "SELECT city FROM customers;",
        ),
        (
            "5. no fence at all — fall back to the first SELECT/WITH",
            "Let me think. The count lives in customers.\n"
            "SELECT COUNT(*) AS n FROM customers;\n"
            "That is the answer.",
            "SELECT COUNT(*) AS n FROM customers;",
        ),
        (
            "5b. no fence, statement starts with WITH",
            "A CTE is clearest here.\n"
            "WITH recent AS (SELECT * FROM orders) SELECT COUNT(*) FROM recent;",
            "WITH recent AS (SELECT * FROM orders) SELECT COUNT(*) FROM recent;",
        ),
        (
            "6. the template's placeholder comment is echoed and stripped",
            f"{SQL_FENCE_OPEN}\n-- Your SQL query\nSELECT 1;\n{FENCE_CLOSE}",
            "SELECT 1;",
        ),
        (
            "7. multi-line SQL keeps its internal formatting",
            f"{SQL_FENCE_OPEN}\nSELECT c.full_name, COUNT(o.order_id)\n"
            f"FROM customers AS c\nLEFT JOIN orders AS o "
            f"ON o.customer_id = c.customer_id\nGROUP BY c.full_name\n{FENCE_CLOSE}",
            "SELECT c.full_name, COUNT(o.order_id)\nFROM customers AS c\n"
            "LEFT JOIN orders AS o ON o.customer_id = c.customer_id\n"
            "GROUP BY c.full_name",
        ),
        (
            "8. empty trailing fence is skipped for the last real one",
            f"{SQL_FENCE_OPEN}\nSELECT 42;\n{FENCE_CLOSE}\n\n{SQL_FENCE_OPEN}\n"
            f"{FENCE_CLOSE}",
            "SELECT 42;",
        ),
    ]

    for label, reply, expected in SHAPES:
        got = extract_sql(reply)
        status = "PASS" if got == expected else "FAIL"
        print(f"  {status}  {label}")
        print(f"        -> {got.replace(chr(10), ' | ')!r}")
        assert got == expected, f"{label}: expected {expected!r}, got {got!r}"

    print()
    for label, bad in [
        ("pure prose, no SQL anywhere", "I am sorry, I cannot answer that."),
        ("empty response", ""),
        ("whitespace only", "   \n\t "),
    ]:
        try:
            extract_sql(bad)
            raise SystemExit(f"FAILED: {label} should have raised")
        except SQLExtractionError as exc:
            print(f"  raises SQLExtractionError  ({label})")
            print(f"        -> {str(exc)[:96]}")

    # ------------------------------------------------------------------
    # Prompt contract + cache behaviour
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("CHECK (d) — prompt contract and DDL cache")
    print(RULE)

    for placeholder in PROMPT_PLACEHOLDERS:
        assert placeholder in OMNISQL_PROMPT_TEMPLATE, placeholder
    print(f"  template still contains {list(PROMPT_PLACEHOLDERS)}")

    built = build_prompt("How many customers are there?", ddl_text, Dialect.SQLITE)
    for placeholder in PROMPT_PLACEHOLDERS:
        assert placeholder not in built, f"{placeholder} was not substituted"
    assert built.startswith("Task Overview:\nYou are a data science expert.")
    assert built.endswith(
        "Take a deep breath and think step by step to find the correct SQL query."
    )
    assert SQL_FENCE_OPEN in built, "the output-format contract must survive"
    assert "How many customers are there?" in built
    assert 'CREATE TABLE "customers"' in built
    print("  built prompt: template preserved, placeholders substituted")

    print("\n  Database Engine line per dialect (this is the multi-dialect swap):")
    for d in DIALECT_SPECS:
        line = build_prompt("q", "CREATE TABLE t (a INTEGER);", d).splitlines()[4]
        print(f"    {d.value:<11} -> {line!r}")
        assert line == DIALECT_SPECS[d].prompt_name

    clear_schema_cache()
    first_ddl, _ = get_schema_ddl(conn, Dialect.SQLITE)
    assert len(_SCHEMA_CACHE) == 1
    second_ddl, _ = get_schema_ddl(conn, Dialect.SQLITE)
    assert len(_SCHEMA_CACHE) == 1, "a second call must reuse the entry"
    assert first_ddl == second_ddl
    print(f"\n  cache: 2 calls -> {len(_SCHEMA_CACHE)} entry, identical DDL")

    refreshed, _ = get_schema_ddl(conn, Dialect.SQLITE, refresh=True)
    assert refreshed == first_ddl
    print("  cache: refresh=True re-introspects and returns the same DDL")

    freed = clear_schema_cache(conn)
    print(f"  cache: clear_schema_cache(conn) dropped {freed} entry")
    assert freed >= 1 and not _SCHEMA_CACHE

    # A dead connection must surface as .error, not as a traceback.
    dead = sqlite3.connect(":memory:")
    dead.close()
    broken = answer_question("How many customers are there?", dead, config=cfg)
    print(f"\n  closed connection -> ok={broken.ok}")
    print(f"        -> {broken.error}")
    assert broken.ok is False and broken.error is not None

    empty = answer_question("   ", conn, config=cfg)
    assert empty.ok is False and empty.error == "empty question"
    print(f"  empty question    -> {empty.error!r}")

    print()
    print(RULE)
    print("ALL CHECKS PASSED")
    print(RULE)
    conn.close()
    for key in ("SCM_MODEL_PROVIDER", "SCM_DIALECT", "SCM_DB_DSN"):
        os.environ.pop(key, None)
