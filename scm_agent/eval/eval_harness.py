"""Execution-accuracy harness — the number that goes in the report.

This file produces the headline table of the final report:

    model stage            easy    medium   hard    overall
    stage-1-mock            x/5     y/5      z/4      n/14
    stage-2-base            ...
    stage-3-finetuned       ...

One row per model stage, all rows produced by *this* code against *this* test
set, so the comparison is apples to apples. Because the project's main claim is
"fine-tuning OmniSQL made it better at our schemas", the scoring rules below are
load-bearing: a harness that is too lenient inflates every row, and a harness
that is too strict makes the fine-tune look worthless. Both are ways to fail the
viva, so each rule is spelled out with its reason.

What is being measured: execution accuracy
------------------------------------------
The standard text-to-SQL metric (Spider, BIRD, and OmniSQL's own model card all
report it). We run the model's SQL and the reference `expected_sql` against the
*same* database and compare the **result sets**, never the query strings.

String comparison would be wrong, not merely strict. All of these are the same
query, and only one of them is the one we happened to write down:

    SELECT COUNT(*) AS customer_count FROM customers
    SELECT COUNT(customer_id) AS n FROM customers
    SELECT COUNT(*) FROM customers AS c

The comparison rules, and why each one exists
---------------------------------------------
1. **Rows are a multiset, not a list** — unless the reference query has a
   top-level `ORDER BY`, in which case the question asked for an order ("list
   them alphabetically", "the top three") and the order is part of the answer.
   Grading unordered queries in order would fail correct SQL for the crime of a
   different join plan; grading ordered queries unordered would pass a model
   that ignored "top 3 by revenue". So the reference query itself decides which
   rule applies — see `reference_is_ordered`.

2. **Column order matters, column names do not.** `AS customer_count` and
   `AS total` are the same answer with different labels, and models legitimately
   alias differently. Position still matters, because
   `SELECT name, price` and `SELECT price, name` are different answers to
   "which products cost the most".

3. **Numbers are compared numerically, not by type.** PostgreSQL returns
   `NUMERIC` as `Decimal("558.00")`; SQLite returns the same value as the float
   `558.0`; a `COUNT` comes back as `int`. All three are the number 558 and all
   three must compare equal, or the entire "money" half of the test set fails
   for a reason that has nothing to do with the model. Floats are compared after
   rounding to `NUMERIC_COMPARISON_PLACES`, because summing the same rows in a
   different order gives `1234.5600000000002` instead of `1234.56` — a float
   artefact, not a wrong answer.

4. **NULL equals NULL and never crashes the sort.** `None < 1` raises
   `TypeError` in Python 3, so any harness that calls `sorted()` on raw rows
   dies on the first `LEFT JOIN` test case. Every value is converted to a
   type-tagged string first (`canonical_value`), so mixed-type and NULL-bearing
   rows compare and sort safely.

The honest bit: cases the *harness* cannot grade
------------------------------------------------
`medium-02` uses `DATE '2024-06-01'`, which is valid PostgreSQL and a syntax
error in SQLite. No PostgreSQL server is reachable on this machine
(docs/TESTING.md), so the **reference** query fails before the model is even
considered. That is an environment limitation, not a wrong answer, and counting
it as one would understate every accuracy number we publish.

So the reference query is executed **first**. If it fails, the case is recorded
as `skipped-reference-failed` and excluded from the denominator, and the printed
summary says so out loud. `accuracy` is over gradeable cases;
`accuracy_including_skipped` is reported alongside it so nobody can accuse the
number of being quietly cherry-picked.

Swapping SQLite for a real PostgreSQL server
--------------------------------------------
`open_evaluation_database()` is the **only** function that knows where the
database comes from. It currently builds an in-memory SQLite database from
`db/schema.sql` (translating `SERIAL PRIMARY KEY` and dropping `CREATE INDEX`),
because that needs no server, no driver and no `sudo`. To evaluate against real
PostgreSQL once `docker compose up` works, replace that one function body with
`psycopg.connect(dsn)` and return `Dialect.POSTGRESQL`. Nothing else in this
file — not the scoring, not the reporting — needs to change, and `medium-02`
will start being graded instead of skipped.

Usage
-----
    python3 -m scm_agent.eval.eval_harness
    python3 -m scm_agent.eval.eval_harness --limit 5
    python3 -m scm_agent.eval.eval_harness --out results/stage1.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time as _time_type, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import Enum
from pathlib import Path

from scm_agent.pipeline.config import AppConfig, DatabaseConfig, Dialect, get_config
from scm_agent.pipeline.model_client import ModelClient, get_model_client
from scm_agent.pipeline.nl_to_sql import (
    DEFAULT_MAX_ROWS,
    NLToSQLResult,
    answer_question,
    clear_schema_cache,
)

# --------------------------------------------------------------------------
# Locations. Derived from this file so the harness works from any cwd.
# --------------------------------------------------------------------------

_EVAL_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _EVAL_DIR.parent                      # scm_agent/

DEFAULT_TEST_SET = _EVAL_DIR / "test_set.json"
DEFAULT_SCHEMA_FILE = _PACKAGE_DIR / "db" / "schema.sql"

NUMERIC_COMPARISON_PLACES: int = 6
"""Decimal places kept when comparing numbers.

Six is far more precision than any money column in the schema needs (2 places)
and still absorbs the float noise that floating-point summation introduces when
two correct queries add the same values in a different order. Raising this to
15 would start failing correct queries; lowering it to 2 would start passing
queries that are genuinely off by a fraction.
"""

MAX_DIFF_ROWS: int = 3
"""How many differing rows to quote in a failure detail. Enough to see *what*
went wrong during error analysis, few enough that the stdout table stays
readable."""


# --------------------------------------------------------------------------
# 1. Outcomes — one closed vocabulary, used by the table and the JSON alike
# --------------------------------------------------------------------------

class Outcome(str, Enum):
    """Exactly what happened to one evaluation case.

    Deliberately *not* a boolean. "The model was wrong" and "the model produced
    a query the safety layer refused" and "the model produced SQL that does not
    parse" are three different findings, and the report's error-analysis section
    is precisely the discussion of which one dominates. Collapsing them into
    "wrong" throws that away, and it is the single most common defect in a
    hand-rolled eval harness.

    A `str` Enum so it serialises to JSON without a custom encoder.
    """

    MATCH = "match"
    """Correct: the result sets agree under the rules in this module."""

    MISMATCH = "result-mismatch"
    """Ran fine, returned the wrong rows. A genuine model error."""

    PARSE_FAILURE = "sql-parse-failure"
    """No SQL could be extracted from the model's reply at all."""

    SAFETY_DENIED = "safety-denied"
    """`safety.check_sql` refused the query. On a read-only benchmark this is
    always a model error (it emitted something that writes), but it is tracked
    separately because a *rise* in this bucket after fine-tuning would mean the
    fine-tune taught the model to write — which is a safety finding, not an
    accuracy one."""

    EXECUTION_ERROR = "sql-execution-error"
    """Valid-looking SQL the database rejected: bad column, bad function, wrong
    dialect. For a SQLite-trained base model evaluated on PostgreSQL this is the
    bucket we *expect* to shrink after fine-tuning (docs/PROJECT_BRIEF.md), so
    it is the most report-relevant failure of all."""

    PIPELINE_ERROR = "pipeline-error"
    """Our infrastructure broke — model server unreachable, introspection
    failed. Not the model's fault and not gradeable; a run containing any of
    these is warned about loudly and should be repeated."""

    SKIPPED_REFERENCE_FAILED = "skipped-reference-failed"
    """The *reference* query would not execute here, so there is nothing to
    compare against. Excluded from the denominator — see the module docstring."""

    @property
    def is_correct(self) -> bool:
        return self is Outcome.MATCH

    @property
    def is_scored(self) -> bool:
        """Whether this case counts toward the accuracy denominator."""
        return self is not Outcome.SKIPPED_REFERENCE_FAILED


DIFFICULTY_ORDER: tuple[str, ...] = ("easy", "medium", "hard")
"""Printing order. Any difficulty not listed here is appended alphabetically, so
adding a `very-hard` tier to the test set does not silently drop it."""


# --------------------------------------------------------------------------
# 2. Value canonicalisation — the foundation the comparison stands on
# --------------------------------------------------------------------------

_NULL_TAG = "0"
_NUMBER_TAG = "1"
_TEXT_TAG = "2"
_BYTES_TAG = "3"
_TAG_SEPARATOR = "\x1f"      # ASCII unit separator: cannot occur in our data
_FIELD_SEPARATOR = "\x1e"    # ASCII record separator: same reason

_QUANTUM = Decimal(1).scaleb(-NUMERIC_COMPARISON_PLACES)


def canonical_value(value: object) -> str:
    """Reduce one database value to a tagged string that can be compared *and*
    sorted safely.

    Everything about the comparison follows from this function, so it is worth
    being explicit about what it deliberately erases and what it preserves.

    Erased (because two correct queries may differ here):
      * numeric *type* — `Decimal("558.00")`, `558.0` and `558` all become
        `"1\\x1f558"`. This is the cross-engine case the test-set author flagged:
        PostgreSQL returns exact `NUMERIC`, SQLite returns a float.
      * numeric noise below `NUMERIC_COMPARISON_PLACES`.
      * date/time *representation* — SQLite stores dates as the text
        `'2024-06-01'`, psycopg returns `datetime.date(2024, 6, 1)`. Both become
        the same ISO text, so the same case grades identically on either engine.
      * `bool` vs `0/1` — SQLite has no boolean type, PostgreSQL does.

    Preserved (because a difference here is a genuinely different answer):
      * NULL, which gets its own tag and is therefore never equal to `0`, to the
        empty string, or to the text `"None"`. It also sorts without raising
        `TypeError`, which is the whole reason for returning a string at all.
      * text content, exactly, including case and internal spacing.
    """
    if value is None:
        return _NULL_TAG + _TAG_SEPARATOR

    # Must precede the numeric branch: bool is a subclass of int in Python, and
    # `True` should canonicalise as the number 1, matching how SQLite stores it.
    if isinstance(value, bool):
        value = int(value)

    number = _as_decimal(value)
    if number is not None:
        return _NUMBER_TAG + _TAG_SEPARATOR + _format_decimal(number)

    if isinstance(value, (datetime, date, _time_type)):
        return _TEXT_TAG + _TAG_SEPARATOR + value.isoformat()

    if isinstance(value, (bytes, bytearray, memoryview)):
        return _BYTES_TAG + _TAG_SEPARATOR + bytes(value).hex()

    return _TEXT_TAG + _TAG_SEPARATOR + str(value)


def _as_decimal(value: object) -> Decimal | None:
    """Return `value` as a `Decimal`, or `None` if it is not a finite number.

    Floats go through `str()` rather than `Decimal(float)` on purpose:
    `Decimal(0.1)` is `0.1000000000000000055511151231257827...`, which then
    disagrees with `Decimal("0.1")` from PostgreSQL. `Decimal(str(0.1))` is
    `Decimal("0.1")` — repr round-tripping is exactly the behaviour we want.

    NaN and infinity return `None` and fall through to the text branch. They
    cannot be quantized, and `NaN != NaN` would make the comparison
    non-reflexive, i.e. a query would not match itself.
    """
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        decimal_value = Decimal(str(value))
        return decimal_value if decimal_value.is_finite() else None
    return None


def _format_decimal(number: Decimal) -> str:
    """One canonical text form per numeric value.

    Rounds to `NUMERIC_COMPARISON_PLACES`, strips trailing zeros, and forces
    plain (non-exponential) notation so `558`, `5.58E+2` and `558.000000` all
    render as `"558"`. The wide precision context keeps `quantize` from raising
    on values with more digits than the default 28-digit context allows.
    """
    try:
        with localcontext() as context:
            context.prec = 60
            quantized = number.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
            if quantized == 0:
                return "0"  # collapse -0 and 0, which format differently
            return format(quantized.normalize(), "f")
    except (InvalidOperation, ValueError, OverflowError):
        # Astronomically large values only. Falling back keeps the harness
        # running rather than crashing a whole evaluation over one row.
        return format(number, "f")


def canonical_row(row: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(canonical_value(value) for value in row)


def sort_key(row: tuple[str, ...]) -> str:
    """A stable, total ordering over already-canonicalised rows.

    Only used for *display* — the comparison itself uses `Counter`, which needs
    no ordering at all. But a diff printed in a stable order is reproducible
    between runs, and a report that quotes a different example row each time it
    is regenerated is not something a supervisor can check.
    """
    return _FIELD_SEPARATOR.join(row)


def render_row(row: tuple[str, ...]) -> str:
    """Strip the type tags for human eyes: `('1\\x1f558',)` -> `558`."""
    return " | ".join(value.split(_TAG_SEPARATOR, 1)[1] or "NULL" for value in row)


# --------------------------------------------------------------------------
# 3. Result sets and the comparison itself
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ResultSet:
    """One query's output: column labels, rows, and whether we stopped early."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool = False

    @property
    def canonical(self) -> tuple[tuple[str, ...], ...]:
        return tuple(canonical_row(row) for row in self.rows)


@dataclass(frozen=True)
class Comparison:
    """The verdict for one pair of result sets, plus why."""

    match: bool
    detail: str | None = None
    """Populated on a mismatch only. This text is what the error-analysis
    section of the report is written from, so it names the actual difference
    (column count, row count, which rows) rather than saying "wrong"."""


def compare_result_sets(
    expected: ResultSet, actual: ResultSet, ordered: bool
) -> Comparison:
    """Decide whether `actual` answers the question as well as `expected` does.

    `ordered` comes from the *reference* query having a top-level `ORDER BY`,
    never from the model's query — otherwise a model could earn an easier grade
    by omitting the `ORDER BY` the question asked for.

    Checked in order of how useful the message is: column shape, then row count,
    then contents. A caller reading "expected 2 columns, got 3" learns more than
    one reading a dump of twelve rows.
    """
    if len(expected.columns) != len(actual.columns):
        return Comparison(
            False,
            f"column count: expected {len(expected.columns)} "
            f"{list(expected.columns)}, got {len(actual.columns)} "
            f"{list(actual.columns)}",
        )

    if len(expected.rows) != len(actual.rows):
        return Comparison(
            False,
            f"row count: expected {len(expected.rows)}, got {len(actual.rows)}",
        )

    expected_rows = expected.canonical
    actual_rows = actual.canonical

    if ordered:
        if expected_rows == actual_rows:
            return Comparison(True)
        if Counter(expected_rows) == Counter(actual_rows):
            # Worth its own message: it means the model understood the data and
            # missed only the sort, which is a different fix from a bad join.
            return Comparison(
                False,
                "same rows, wrong order (the reference query has a top-level "
                "ORDER BY, so row order is graded)",
            )
        return Comparison(False, _describe_difference(expected_rows, actual_rows))

    if Counter(expected_rows) == Counter(actual_rows):
        return Comparison(True)
    return Comparison(False, _describe_difference(expected_rows, actual_rows))


def _describe_difference(
    expected_rows: tuple[tuple[str, ...], ...],
    actual_rows: tuple[tuple[str, ...], ...],
) -> str:
    """Name the rows that differ, as a multiset difference in both directions.

    Both directions matter: rows the model missed and rows it invented are
    different bugs (a too-narrow filter versus a fan-out from a bad join).
    """
    missing = Counter(expected_rows) - Counter(actual_rows)
    extra = Counter(actual_rows) - Counter(expected_rows)
    parts: list[str] = []
    for label, counter in (("missing", missing), ("unexpected", extra)):
        if not counter:
            continue
        rows = sorted(counter.elements(), key=sort_key)
        shown = "; ".join(render_row(row) for row in rows[:MAX_DIFF_ROWS])
        more = (
            f" (+{len(rows) - MAX_DIFF_ROWS} more)"
            if len(rows) > MAX_DIFF_ROWS
            else ""
        )
        parts.append(f"{label} {len(rows)} row(s): {shown}{more}")
    return "content differs — " + "; ".join(parts) if parts else "content differs"


# --------------------------------------------------------------------------
# 4. Does the reference query care about row order?
# --------------------------------------------------------------------------

_ORDER_BY = re.compile(r"(?i)\bORDER\s+BY\b")


def _mask_sql(sql: str) -> tuple[str, list[int]]:
    """Blank out literals/comments and record parenthesis depth per character.

    Written here rather than reused from `safety.py` because that module's
    stripper deliberately does not track nesting depth — it does not need to,
    and adding depth tracking to a security-critical file to serve a reporting
    concern would be the wrong trade.

    Returns `(masked_text, depths)` where `depths[i]` is the paren depth at
    character `i`, and every character inside a string literal, quoted
    identifier or comment has been replaced by a space (so a table literally
    named `"order by"` cannot be mistaken for a clause).
    """
    masked: list[str] = []
    depths: list[int] = []
    depth = 0
    index = 0
    length = len(sql)

    def blank(count: int) -> None:
        masked.extend(" " * count)
        depths.extend([depth] * count)

    while index < length:
        char = sql[index]

        if char in ("'", '"', "`"):
            quote = char
            blank(1)
            index += 1
            while index < length:
                if sql[index] == quote:
                    # A doubled quote is an escaped one; stay inside the literal.
                    if index + 1 < length and sql[index + 1] == quote:
                        blank(2)
                        index += 2
                        continue
                    blank(1)
                    index += 1
                    break
                blank(1)
                index += 1
            continue

        if sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            blank(end - index)
            index = end
            continue

        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            blank(end - index)
            index = end
            continue

        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)

        masked.append(char)
        depths.append(depth)
        index += 1

    return "".join(masked), depths


def reference_is_ordered(sql: str) -> bool:
    """True when `sql` has an `ORDER BY` at the outermost query level.

    Depth matters. `ORDER BY` inside a subquery, a derived table or a window
    function's `OVER (...)` does not make the *final* result ordered — the outer
    query is free to return those rows in any order, and SQL engines do. Grading
    such a case in order would fail correct queries at random depending on the
    plan the optimiser picked. `hard-04` in the test set has both an inner and
    an outer aggregate, which is exactly the shape this guards against.

    Known limitation, stated rather than hidden: when a reference query orders
    by a column containing ties (or applies `LIMIT` across a tie), the reference
    row order is one of several legal ones, and a correct model query that broke
    the tie differently is scored as a mismatch. The alternative — ignoring
    order entirely — would pass a model that ignored "top three by revenue", so
    this is the safer of the two errors. Every affected case is visible in the
    per-case detail as "same rows, wrong order".
    """
    masked, depths = _mask_sql(sql)
    return any(depths[match.start()] == 0 for match in _ORDER_BY.finditer(masked))


# --------------------------------------------------------------------------
# 5. The database fixture — the single function to change for PostgreSQL
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Fixture:
    """An open connection plus the dialect its SQL must be written in."""

    connection: object
    dialect: Dialect
    description: str


_SERIAL_PK = re.compile(r"(?i)\bSERIAL\s+PRIMARY\s+KEY\b")
_CREATE_INDEX = re.compile(r"(?is)\bCREATE\s+INDEX\b.*?;")


def open_evaluation_database(schema_file: Path = DEFAULT_SCHEMA_FILE) -> Fixture:
    """Build the database every query in this run executes against.

    **This is the one-function swap point for PostgreSQL.** Everything else in
    this module takes a DB-API 2.0 connection and a `Dialect` and does not care
    where they came from. When `docker compose up -d` finally works
    (docs/TESTING.md), replace the body with:

        import psycopg
        return Fixture(psycopg.connect(dsn), Dialect.POSTGRESQL, dsn)

    and the run immediately grades `medium-02` — whose `DATE '2024-06-01'`
    literal is the reason that case is currently skipped — with no change to any
    scoring code.

    Until then: an in-memory SQLite database loaded from the project's real
    `db/schema.sql`, with the two translations PostgreSQL DDL needs:

      * `SERIAL PRIMARY KEY` -> `INTEGER PRIMARY KEY`, SQLite's rowid alias,
        which auto-increments identically. Without this the seed `INSERT`s,
        which omit the id column, fail.
      * `CREATE INDEX` statements dropped. They are valid SQLite but irrelevant
        to whether a query returns the right rows.

    Loading the *real* schema file rather than a copy is the point: when a
    teammate adds a table, this evaluation starts covering it instead of
    quietly drifting out of date.
    """
    script = schema_file.read_text(encoding="utf-8")
    script = _SERIAL_PK.sub("INTEGER PRIMARY KEY", script)
    script = _CREATE_INDEX.sub("", script)

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(script)
    connection.commit()
    return Fixture(
        connection=connection,
        dialect=Dialect.SQLITE,
        description=f"in-memory SQLite built from {schema_file.name}",
    )


def execute_sql(conn, sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> ResultSet:
    """Run trusted SQL (the reference query) and return its result set.

    No safety check here, deliberately: `expected_sql` is written by us, is
    already under review, and running it through the read-only gate would mean
    a bug in `safety.py` could silently turn every case into a skip. The
    *model's* SQL never takes this path — it goes through `answer_question`,
    which checks it.

    Fetches one row past the ceiling so "there were more" is measured rather
    than assumed, and rolls back afterwards for the same reason `nl_to_sql`
    does: an idle-in-transaction connection is precisely the anomaly Pillar 2
    hunts, and Pillar 1 should not be generating them.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        if cursor.description is None:
            return ResultSet((), (), False)
        columns = tuple(str(column[0]) for column in cursor.description)
        fetched = cursor.fetchmany(max_rows + 1)
        rows = tuple(tuple(row) for row in fetched[:max_rows])
        return ResultSet(columns, rows, len(fetched) > max_rows)
    finally:
        for close in (cursor.close, getattr(conn, "rollback", lambda: None)):
            try:
                close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 6. The test set
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalCase:
    """One question/reference-SQL pair, exactly as `test_set.json` holds it."""

    id: str
    difficulty: str
    question: str
    expected_sql: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestSet:
    dialect: str
    """The dialect the reference SQL is *written in* — not necessarily the one
    it is executed against. When these differ, dialect-specific reference
    queries are expected to land in the skipped bucket, and the summary prints
    both so the discrepancy can never go unnoticed."""

    cases: tuple[EvalCase, ...]
    path: Path


def load_test_set(path: Path = DEFAULT_TEST_SET, limit: int | None = None) -> TestSet:
    """Read and validate `test_set.json`.

    Validation is loud on purpose. A duplicated `id` would make two rows of the
    report collide, and a missing `expected_sql` would silently become an
    ungradeable case — both are much cheaper to catch here than in the writeup.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_cases = payload.get("cases", [])
    if not raw_cases:
        raise ValueError(f"{path} contains no cases")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases):
        case_id = str(raw.get("id") or f"case-{index + 1}")
        if case_id in seen:
            raise ValueError(f"{path}: duplicate case id {case_id!r}")
        seen.add(case_id)
        if not raw.get("expected_sql"):
            raise ValueError(f"{path}: case {case_id!r} has no expected_sql")
        if not raw.get("question"):
            raise ValueError(f"{path}: case {case_id!r} has no question")
        cases.append(
            EvalCase(
                id=case_id,
                difficulty=str(raw.get("difficulty", "unknown")),
                question=str(raw["question"]),
                expected_sql=str(raw["expected_sql"]),
                tags=tuple(str(tag) for tag in raw.get("tags", ())),
            )
        )

    if limit is not None:
        cases = cases[:limit]
    return TestSet(
        dialect=str(payload.get("dialect", "unknown")),
        cases=tuple(cases),
        path=path,
    )


# --------------------------------------------------------------------------
# 7. Running one case
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseResult:
    """The complete record of one graded case. Frozen: it is evidence.

    Everything needed to reconstruct the row of the report *and* to argue with
    it later — the generated SQL, the safety verdict, the failure detail — is
    here, because "the model got 6/14" is not a finding until someone can see
    which six.
    """

    id: str
    difficulty: str
    question: str
    tags: tuple[str, ...]
    expected_sql: str
    generated_sql: str | None
    executed_sql: str | None
    safety_allowed: bool | None
    """`None` means the safety layer was never reached (no SQL was produced)."""

    safety_reason: str | None
    outcome: Outcome
    ordered_comparison: bool
    """Whether row order was graded, i.e. whether the reference has a top-level
    ORDER BY. Recorded per case so the grading rule applied is auditable."""

    expected_rows: int | None
    actual_rows: int | None
    detail: str | None
    error: str | None
    duration_ms: float
    """Time inside `answer_question` — prompt build, model call, safety check
    and execution. Comparable across stages, which the wall-clock time of the
    whole case (dominated by reference execution on a mock run) would not be."""

    reference_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "difficulty": self.difficulty,
            "question": self.question,
            "tags": list(self.tags),
            "expected_sql": self.expected_sql,
            "generated_sql": self.generated_sql,
            "executed_sql": self.executed_sql,
            "safety_allowed": self.safety_allowed,
            "safety_reason": self.safety_reason,
            "outcome": self.outcome.value,
            "correct": self.outcome.is_correct,
            "scored": self.outcome.is_scored,
            "ordered_comparison": self.ordered_comparison,
            "expected_rows": self.expected_rows,
            "actual_rows": self.actual_rows,
            "detail": self.detail,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "reference_ms": round(self.reference_ms, 2),
        }


_PARSE_FAILURE_MARKERS: tuple[str, ...] = (
    "no SQL found in the model response",
    "the model returned an empty response",
    "SQL extraction failed",
)
"""Substrings `nl_to_sql` uses when the *model's reply* was unusable.

Matching on message text is admittedly a soft coupling, so the default is
chosen to fail safe: an error we do not recognise is classified as
`PIPELINE_ERROR` — ours — rather than blamed on the model. An over-reported
pipeline error costs a re-run; an under-reported one is a false accuracy figure.
"""


def classify(result: NLToSQLResult) -> Outcome:
    """Map a pipeline result onto the failure vocabulary, before any comparison.

    Order matters: the safety verdict is checked first because a denied query
    also has `error` set, and "blocked by the safety layer" is the more precise
    of the two facts.
    """
    if result.safety is not None and not result.safety.allowed:
        return Outcome.SAFETY_DENIED
    if result.sql is None:
        error = result.error or ""
        if any(marker in error for marker in _PARSE_FAILURE_MARKERS):
            return Outcome.PARSE_FAILURE
        return Outcome.PIPELINE_ERROR
    if result.error is not None:
        # SQL was produced and allowed, but the database refused it: syntax,
        # unknown column, wrong dialect.
        return Outcome.EXECUTION_ERROR
    return Outcome.MISMATCH  # provisional — the comparison decides


def run_case(
    case: EvalCase,
    conn,
    config: AppConfig,
    client: ModelClient,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> CaseResult:
    """Grade one case. Never raises — a harness crash mid-run loses every result.

    The reference query runs **first and unconditionally**. If it cannot execute
    in this environment there is nothing to grade against, and the case is
    skipped rather than charged to the model. See the module docstring for why
    that distinction decides whether the published accuracy is honest.
    """
    reference_started = time.perf_counter()
    reference: ResultSet | None = None
    reference_error: str | None = None
    try:
        reference = execute_sql(conn, case.expected_sql, max_rows)
    except Exception as exc:
        reference_error = f"{type(exc).__name__}: {exc}"
    reference_ms = (time.perf_counter() - reference_started) * 1000

    ordered = reference_is_ordered(case.expected_sql)

    if reference is None:
        return CaseResult(
            id=case.id,
            difficulty=case.difficulty,
            question=case.question,
            tags=case.tags,
            expected_sql=case.expected_sql,
            generated_sql=None,
            executed_sql=None,
            safety_allowed=None,
            safety_reason=None,
            outcome=Outcome.SKIPPED_REFERENCE_FAILED,
            ordered_comparison=ordered,
            expected_rows=None,
            actual_rows=None,
            detail=(
                "the reference query itself failed to execute against "
                f"{config.database.dialect.value} — harness/environment "
                "limitation, NOT a model error, excluded from accuracy"
            ),
            error=reference_error,
            duration_ms=0.0,
            reference_ms=reference_ms,
        )

    result = answer_question(
        case.question, conn, config=config, client=client, max_rows=max_rows
    )

    outcome = classify(result)
    detail: str | None = None
    actual = ResultSet(result.columns, result.rows, result.truncated)

    if outcome is Outcome.MISMATCH:
        comparison = compare_result_sets(reference, actual, ordered)
        outcome = Outcome.MATCH if comparison.match else Outcome.MISMATCH
        detail = comparison.detail
        if reference.truncated or actual.truncated:
            # A truncated comparison is not a comparison. Say so rather than
            # reporting a match that only holds for the first `max_rows` rows.
            note = f"result truncated at max_rows={max_rows}; comparison is partial"
            detail = f"{detail}; {note}" if detail else note

    return CaseResult(
        id=case.id,
        difficulty=case.difficulty,
        question=case.question,
        tags=case.tags,
        expected_sql=case.expected_sql,
        generated_sql=result.sql,
        executed_sql=result.executed_sql,
        safety_allowed=None if result.safety is None else result.safety.allowed,
        safety_reason=None if result.safety is None else result.safety.reason,
        outcome=outcome,
        ordered_comparison=ordered,
        expected_rows=len(reference.rows),
        actual_rows=len(actual.rows),
        detail=detail,
        error=result.error,
        duration_ms=result.duration_ms,
        reference_ms=reference_ms,
    )


# --------------------------------------------------------------------------
# 8. Aggregation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Breakdown:
    """Accuracy for one slice of the test set (a difficulty tier, or a tag)."""

    label: str
    total: int
    scored: int
    correct: int
    skipped: int

    @property
    def accuracy(self) -> float | None:
        """`None`, not 0.0, when nothing in this slice was gradeable.

        A slice where every reference query failed has no accuracy. Printing
        0.0 there would read as "the model got them all wrong", which is a
        false statement about the model.
        """
        return self.correct / self.scored if self.scored else None

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "total": self.total,
            "scored": self.scored,
            "correct": self.correct,
            "skipped": self.skipped,
            "accuracy": None if self.accuracy is None else round(self.accuracy, 4),
        }


def _breakdown(label: str, results: list[CaseResult]) -> Breakdown:
    scored = [r for r in results if r.outcome.is_scored]
    return Breakdown(
        label=label,
        total=len(results),
        scored=len(scored),
        correct=sum(1 for r in scored if r.outcome.is_correct),
        skipped=len(results) - len(scored),
    )


@dataclass(frozen=True)
class EvalRun:
    """A whole evaluation: what ran, against what, and how it scored."""

    stage: str
    """`ModelConfig.stage` — "stage-1-mock" / "stage-2-base" /
    "stage-3-finetuned". The most important field in the file. Three rows of the
    report's headline table differ only by this string, and a results file that
    did not carry it could be attributed to the wrong model at the viva, which
    would invalidate the project's central claim."""

    provider: str
    served_model: str
    lora_adapter: str | None
    started_at: str
    test_set_path: str
    test_set_dialect: str
    executed_dialect: str
    database: str
    results: tuple[CaseResult, ...] = ()
    duration_s: float = 0.0

    @property
    def scored(self) -> list[CaseResult]:
        return [r for r in self.results if r.outcome.is_scored]

    @property
    def correct(self) -> int:
        return sum(1 for r in self.scored if r.outcome.is_correct)

    @property
    def skipped(self) -> int:
        return len(self.results) - len(self.scored)

    @property
    def accuracy(self) -> float | None:
        """The headline number: correct / gradeable."""
        scored = self.scored
        return self.correct / len(scored) if scored else None

    @property
    def accuracy_including_skipped(self) -> float | None:
        """The same number with skipped cases counted as failures.

        Published alongside the headline so the choice of denominator is
        visible instead of implied. Whenever the two differ, the run is telling
        you that the evaluation environment — not the model — is the reason.
        """
        return self.correct / len(self.results) if self.results else None

    def by_difficulty(self) -> list[Breakdown]:
        groups: dict[str, list[CaseResult]] = {}
        for result in self.results:
            groups.setdefault(result.difficulty, []).append(result)
        ordered = [d for d in DIFFICULTY_ORDER if d in groups]
        ordered += sorted(set(groups) - set(DIFFICULTY_ORDER))
        return [_breakdown(name, groups[name]) for name in ordered]

    def by_tag(self) -> list[Breakdown]:
        groups: dict[str, list[CaseResult]] = {}
        for result in self.results:
            for tag in result.tags:
                groups.setdefault(tag, []).append(result)
        return [_breakdown(tag, groups[tag]) for tag in sorted(groups)]

    def by_outcome(self) -> dict[str, int]:
        """Every outcome that occurred, most common first."""
        return dict(Counter(r.outcome.value for r in self.results).most_common())

    def failure_reasons(self) -> dict[str, int]:
        """Outcome counts for graded cases that were not correct.

        This is the error-analysis table. "6 wrong" is not actionable; "4 wrong
        rows, 1 dialect error, 1 unparseable" tells the next person what to fix.
        """
        return dict(
            Counter(
                r.outcome.value for r in self.scored if not r.outcome.is_correct
            ).most_common()
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run": {
                "stage": self.stage,
                "provider": self.provider,
                "served_model": self.served_model,
                "lora_adapter": self.lora_adapter,
                "started_at": self.started_at,
                "duration_s": round(self.duration_s, 2),
                "test_set": self.test_set_path,
                "test_set_dialect": self.test_set_dialect,
                "executed_dialect": self.executed_dialect,
                "database": self.database,
            },
            "summary": {
                "total_cases": len(self.results),
                "scored_cases": len(self.scored),
                "correct": self.correct,
                "skipped_reference_failed": self.skipped,
                "accuracy": None if self.accuracy is None else round(self.accuracy, 4),
                "accuracy_including_skipped": (
                    None
                    if self.accuracy_including_skipped is None
                    else round(self.accuracy_including_skipped, 4)
                ),
                "by_outcome": self.by_outcome(),
                "failure_reasons": self.failure_reasons(),
                "by_difficulty": [b.to_dict() for b in self.by_difficulty()],
                "by_tag": [b.to_dict() for b in self.by_tag()],
            },
            "cases": [r.to_dict() for r in self.results],
        }


# --------------------------------------------------------------------------
# 9. Driving a whole run
# --------------------------------------------------------------------------

def run_evaluation(
    test_set: TestSet,
    fixture: Fixture,
    config: AppConfig,
    client: ModelClient | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    progress: bool = True,
) -> EvalRun:
    """Grade every case and assemble the run record.

    One `ModelClient` is built and reused for the whole run. At stage 1 that is
    merely tidy; at stages 2 and 3 it is the difference between one HTTP
    connection and one per question.
    """
    if client is None:
        client = get_model_client(config.model)

    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results: list[CaseResult] = []

    for index, case in enumerate(test_set.cases, start=1):
        result = run_case(case, fixture.connection, config, client, max_rows)
        results.append(result)
        if progress:
            flag = "ok " if result.outcome.is_correct else "   "
            print(
                f"  [{index:>2}/{len(test_set.cases)}] {flag}{case.id:<10} "
                f"{result.outcome.value}",
                flush=True,
            )

    return EvalRun(
        stage=config.model.stage,
        provider=config.model.provider.value,
        served_model=config.model.served_model,
        lora_adapter=config.model.lora_adapter,
        started_at=started_at,
        test_set_path=str(test_set.path),
        test_set_dialect=test_set.dialect,
        executed_dialect=fixture.dialect.value,
        database=fixture.description,
        results=tuple(results),
        duration_s=time.perf_counter() - started,
    )


# --------------------------------------------------------------------------
# 10. Reporting to stdout
# --------------------------------------------------------------------------

RULE = "=" * 78


def _percent(value: float | None) -> str:
    return "  n/a " if value is None else f"{value * 100:5.1f}%"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def print_report(run: EvalRun) -> None:
    """Print the run as a table a human can read and a supervisor can check."""
    print()
    print(RULE)
    print("EVALUATION RUN")
    print(RULE)
    print(f"  model stage      : {run.stage}")
    print(f"  provider / model : {run.provider} / {run.served_model}")
    print(f"  lora adapter     : {run.lora_adapter or '(none)'}")
    print(f"  test set         : {run.test_set_path}")
    print(f"  reference dialect: {run.test_set_dialect}")
    print(f"  executed against : {run.executed_dialect} — {run.database}")
    if run.test_set_dialect != run.executed_dialect:
        print(
            f"  NOTE             : reference SQL is written for "
            f"{run.test_set_dialect} but executed on {run.executed_dialect}; "
            "dialect-specific reference queries are skipped, not failed"
        )
    print(f"  started          : {run.started_at}  ({run.duration_s:.2f}s)")

    print()
    print(RULE)
    print("PER-CASE RESULTS")
    print(RULE)
    header = (
        f"  {'id':<10} {'diff':<7} {'outcome':<24} {'ord':<4} "
        f"{'exp':>4} {'got':>4} {'ms':>7}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for result in run.results:
        mark = "PASS" if result.outcome.is_correct else "    "
        expected_rows = "-" if result.expected_rows is None else result.expected_rows
        actual_rows = "-" if result.actual_rows is None else result.actual_rows
        print(
            f"  {result.id:<10} {result.difficulty:<7} "
            f"{result.outcome.value:<24} "
            f"{'yes' if result.ordered_comparison else 'no':<4} "
            f"{expected_rows:>4} {actual_rows:>4} "
            f"{result.duration_ms:>7.1f}  {mark}"
        )

    failures = [r for r in run.results if not r.outcome.is_correct]
    if failures:
        print()
        print(RULE)
        print("WHY EACH NON-MATCHING CASE DID NOT MATCH")
        print(RULE)
        for result in failures:
            print(f"\n  {result.id}  [{result.outcome.value}]  {result.question}")
            if result.generated_sql:
                flat = " ".join(result.generated_sql.split())
                print(f"    generated : {_truncate(flat, 150)}")
            if result.detail:
                print(f"    detail    : {_truncate(result.detail, 150)}")
            if result.error:
                flat_error = " ".join(result.error.split())
                print(f"    error     : {_truncate(flat_error, 150)}")

    print()
    print(RULE)
    print("ACCURACY BY DIFFICULTY")
    print(RULE)
    print(f"  {'tier':<10} {'correct':>8} {'scored':>7} {'skipped':>8} {'accuracy':>9}")
    print("  " + "-" * 46)
    for breakdown in run.by_difficulty():
        print(
            f"  {breakdown.label:<10} {breakdown.correct:>8} {breakdown.scored:>7} "
            f"{breakdown.skipped:>8} {_percent(breakdown.accuracy):>9}"
        )
    print("  " + "-" * 46)
    print(
        f"  {'OVERALL':<10} {run.correct:>8} {len(run.scored):>7} "
        f"{run.skipped:>8} {_percent(run.accuracy):>9}"
    )

    print()
    print(RULE)
    print("ACCURACY BY TAG")
    print(RULE)
    for breakdown in run.by_tag():
        skipped_note = f"   ({breakdown.skipped} skipped)" if breakdown.skipped else ""
        print(
            f"  {breakdown.label:<24} {breakdown.correct:>3}/{breakdown.scored:<3} "
            f"{_percent(breakdown.accuracy)}{skipped_note}"
        )

    print()
    print(RULE)
    print("FAILURE BREAKDOWN  (the error-analysis table)")
    print(RULE)
    reasons = run.failure_reasons()
    if reasons:
        for reason, count in reasons.items():
            print(f"  {reason:<26} {count:>3}")
    else:
        print("  (no graded failures)")
    if run.skipped:
        print(
            f"  {Outcome.SKIPPED_REFERENCE_FAILED.value:<26} {run.skipped:>3}"
            "   <- NOT counted as wrong (reference query would not run here)"
        )

    print()
    print(RULE)
    print("HEADLINE")
    print(RULE)
    print(
        f"  {run.stage}: {run.correct}/{len(run.scored)} gradeable cases correct "
        f"= {_percent(run.accuracy).strip()} execution accuracy"
    )
    if run.skipped:
        print(
            f"  ({run.skipped} of {len(run.results)} case(s) skipped: the reference "
            f"query itself does not run on {run.executed_dialect}. Counting those "
            f"as failures instead would give "
            f"{_percent(run.accuracy_including_skipped).strip()} — shown only so "
            "the denominator is never in doubt.)"
        )
    pipeline_errors = sum(1 for r in run.results if r.outcome is Outcome.PIPELINE_ERROR)
    if pipeline_errors:
        print(
            f"  WARNING: {pipeline_errors} case(s) failed inside our own pipeline "
            "(model server, introspection). Those are not model errors — fix the "
            "environment and re-run before quoting this number."
        )
    print(RULE)


# --------------------------------------------------------------------------
# 11. Entry point
# --------------------------------------------------------------------------

def build_config(fixture: Fixture) -> AppConfig:
    """Model settings from the environment, database settings from the fixture.

    The model half must come from the environment — that is `config.py`'s whole
    reason for existing as the single swap point, and hardcoding a provider here
    would quietly bypass it. The database half must come from the fixture,
    because the prompt tells the model which engine to write for and the safety
    layer applies that engine's comment rules. If those disagreed with the
    connection we actually execute on, the model would be asked for PostgreSQL
    and graded on SQLite, and every dialect difference would be scored as a
    model error.
    """
    env = get_config()
    return AppConfig(
        model=env.model,
        database=DatabaseConfig(dsn=fixture.description, dialect=fixture.dialect),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scm_agent.eval.eval_harness",
        description=(
            "Execution-accuracy evaluation for the NL->SQL pipeline. Runs each "
            "question through the configured model and compares the resulting "
            "rows against the reference query's rows."
        ),
    )
    parser.add_argument(
        "--test-set",
        type=Path,
        default=DEFAULT_TEST_SET,
        help="path to a test set JSON file (default: %(default)s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="only run the first N cases — for a quick smoke run",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the full results (run metadata, per-case records, summary) "
             "to this JSON file",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help="row ceiling per query (default: %(default)s)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the per-case progress lines while running",
    )
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    test_set = load_test_set(args.test_set, limit=args.limit)
    fixture = open_evaluation_database()
    config = build_config(fixture)

    print(RULE)
    print(
        f"Running {len(test_set.cases)} case(s) at {config.model.stage} against "
        f"{fixture.description}"
    )
    print(RULE)
    try:
        run = run_evaluation(
            test_set,
            fixture,
            config,
            max_rows=args.max_rows,
            progress=not args.quiet,
        )
    finally:
        # The DDL cache holds a reference to the connection on purpose (see
        # nl_to_sql._SCHEMA_CACHE); dropping it here keeps repeated in-process
        # runs from accumulating dead connections.
        clear_schema_cache(fixture.connection)
        try:
            fixture.connection.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    print_report(run)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(run.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nresults written to {args.out}")

    # Exit code reports harness health, not model accuracy. A low score is a
    # finding to write up; a pipeline error means the run itself is invalid, and
    # only that should ever break a script or a CI step.
    return 1 if any(r.outcome is Outcome.PIPELINE_ERROR for r in run.results) else 0


if __name__ == "__main__":
    sys.exit(main())
