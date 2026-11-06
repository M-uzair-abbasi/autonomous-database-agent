"""Execution accuracy for `ask`: the number Term I has to deliver (CLAUDE.md section 10).

    python -m modules.ask.evaluate setup     # (re)build the scm_eval database
    python -m modules.ask.evaluate run       # score the test set, write a report

The DSN comes from `SCM_EVAL_DSN` only, never the command line (it carries a
password, and command lines are visible to every user on the machine). It must
name the `scm_eval` database: setup drops that database and nothing else, and
a run refuses any other.

What makes the number trustworthy, each fixing a finding from the GPT review:

* Preflight (I8). Every reference query must run before the model is asked
  anything. A broken reference stops the run; it never quietly shrinks the
  denominator.
* Failure attribution (I7). A dead model server or database is the
  infrastructure's fault: the case is unscored and the whole run is marked
  invalid. A reply with no SQL, or SQL the validator or database refuses, is
  the model's fault and counts as wrong.
* No partial credit (C2). The reference fits under the row cap (preflight
  checks), so a candidate that overflows the cap has more rows than the
  reference and is wrong, not inconclusive.
* Same snapshot (I10). Reference and candidate run on one connection to a
  database seeded from a file whose hash is stamped on the database and
  checked before every run.
* Provenance (I10). Every report records the git commit, the test set and
  schema hashes, and the model stage and name. Never the DSN.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import sqlglot

from contracts import ExecutionResult, SQLCandidate
from modules.ask.generate import GenerationError, generate_candidate
from modules.ask.stubs.execute import execute
from scm_agent.eval.eval_harness import ResultSet, compare_result_sets
from scm_agent.pipeline.config import DIALECT_SPECS, AppConfig, Dialect, get_config
from scm_agent.pipeline.model_client import ModelClient, get_model_client

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_SET = REPO_ROOT / "scm_agent" / "eval" / "test_set.json"
EVAL_DATABASE = "scm_eval"
DEFAULT_EVAL_DSN = f"postgresql://scm:scm@localhost:5434/{EVAL_DATABASE}"
"""The disposable compose instance (docker-compose.yml). No other default."""

DEFAULT_MAX_ROWS = 1000
"""Comfortably above every reference result in the project test set; preflight
says so if a reference ever outgrows it."""

DIFFICULTIES = ("easy", "medium", "hard")
_SUITE_KEYS = {"dialect", "schema_file", "cases"}
_CASE_KEYS = {"id", "difficulty", "question", "expected_sql", "tags"}
_SEED_STAMP = "scm_eval seeded from schema sha256 "


class Outcome(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NO_SQL = "no-sql"
    """The model replied without a usable query."""

    REJECTED = "rejected"
    """The validator or the database refused the query the model wrote."""

    INFRA_ERROR = "infra-error"
    """The model server or the database failed. Not scored; the run is invalid."""

    @property
    def scored(self) -> bool:
        return self is not Outcome.INFRA_ERROR


# --------------------------------------------------------------------------
# The test set
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    id: str
    difficulty: str
    question: str
    expected_sql: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Suite:
    path: Path
    sha256: str
    dialect: Dialect
    """The dialect the reference SQL is written in. A run must use the same one."""

    schema_file: Path
    schema_sha256: str
    cases: tuple[Case, ...]


def load_suite(path: Path = DEFAULT_TEST_SET) -> Suite:
    """Read the test set and refuse anything malformed (GPT review I10).

    Strict on purpose: a duplicate id collides in the report, a duplicate tag
    counts a case twice in the tag breakdown, and a misspelled key silently
    drops a field. Each is cheaper to catch here than in the thesis.
    """
    path = Path(path)
    raw = path.read_bytes()
    payload = json.loads(raw)

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(f"{path}: {message}")

    require(isinstance(payload, dict), "top level must be an object")
    require(set(payload) <= _SUITE_KEYS, f"unknown keys {sorted(set(payload) - _SUITE_KEYS)}")
    require("dialect" in payload and "schema_file" in payload, "needs dialect and schema_file")
    try:
        dialect = Dialect(payload["dialect"])
    except ValueError:
        raise ValueError(f"{path}: unknown dialect {payload['dialect']!r}") from None
    schema_file = REPO_ROOT / payload["schema_file"]
    require(schema_file.is_file(), f"schema_file {payload['schema_file']!r} does not exist")

    raw_cases = payload.get("cases")
    require(isinstance(raw_cases, list) and raw_cases, "needs a non-empty cases list")

    cases: list[Case] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_cases):
        where = f"case #{index + 1}"
        require(isinstance(item, dict), f"{where} must be an object")
        require(set(item) == _CASE_KEYS, f"{where} must have exactly the keys {sorted(_CASE_KEYS)}")
        for key in ("id", "question", "expected_sql"):
            require(isinstance(item[key], str) and item[key].strip(), f"{where}: blank {key}")
        case_id = item["id"]
        require(case_id not in seen, f"duplicate id {case_id!r}")
        seen.add(case_id)
        require(item["difficulty"] in DIFFICULTIES, f"{case_id}: difficulty must be one of {DIFFICULTIES}")
        tags = item["tags"]
        require(
            isinstance(tags, list) and tags and all(isinstance(t, str) and t.strip() for t in tags),
            f"{case_id}: tags must be a non-empty list of names",
        )
        require(len(set(tags)) == len(tags), f"{case_id}: duplicate tags")
        cases.append(
            Case(case_id, item["difficulty"], item["question"], item["expected_sql"], tuple(tags))
        )

    return Suite(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        dialect=dialect,
        schema_file=schema_file,
        schema_sha256=hashlib.sha256(schema_file.read_bytes()).hexdigest(),
        cases=tuple(cases),
    )


def is_ordered(sql: str, dialect: Dialect) -> bool:
    """True when `sql` has an ORDER BY on its outermost query.

    Row order is graded only then. An ORDER BY inside a subquery, a CTE or a
    window does not order the final result. Parsed with sqlglot rather than
    scanned as text, so `$$ ORDER BY $$` in a string cannot fool it (M5).
    """
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT_SPECS[dialect].sqlglot_name)
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"does not parse: {exc}") from exc
    return tree is not None and tree.args.get("order") is not None


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


class PreflightError(RuntimeError):
    """One or more reference queries cannot be graded against. Nothing was run."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "preflight failed, the model was not asked anything:\n  " + "\n  ".join(problems)
        )
        self.problems = problems


@dataclass(frozen=True)
class Reference:
    result: ExecutionResult
    ordered: bool


@dataclass(frozen=True)
class CaseResult:
    case: Case
    outcome: Outcome
    detail: str | None
    sql: str | None
    raw_response: str
    generation_ms: float
    execution_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "difficulty": self.case.difficulty,
            "tags": list(self.case.tags),
            "question": self.case.question,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "sql": self.sql,
            "expected_sql": self.case.expected_sql,
            "raw_response": self.raw_response,
            "generation_ms": round(self.generation_ms, 1),
            "execution_ms": round(self.execution_ms, 1),
        }


@dataclass(frozen=True)
class Run:
    results: tuple[CaseResult, ...]
    provenance: dict[str, Any]

    @property
    def scored(self) -> list[CaseResult]:
        return [r for r in self.results if r.outcome.scored]

    @property
    def correct(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.MATCH)

    @property
    def accuracy(self) -> float | None:
        """Correct over scored. None when nothing could be scored."""
        return self.correct / len(self.scored) if self.scored else None

    @property
    def valid(self) -> bool:
        """False if any case hit an infrastructure failure. Repeat such a run."""
        return all(r.outcome.scored for r in self.results)

    def breakdown(self, by: str) -> dict[str, tuple[int, int]]:
        """`{label: (correct, scored)}` by "difficulty" or by "tag"."""
        counts: dict[str, list[int]] = {}
        for result in self.scored:
            labels = result.case.tags if by == "tag" else (result.case.difficulty,)
            for label in labels:
                entry = counts.setdefault(label, [0, 0])
                entry[0] += result.outcome is Outcome.MATCH
                entry[1] += 1
        return {label: (c, s) for label, (c, s) in sorted(counts.items())}

    def latency(self) -> dict[str, float | None]:
        """p50 and p95 of generation + execution time, over cases that ran."""
        times = sorted(r.generation_ms + r.execution_ms for r in self.scored)
        return {"p50": _percentile(times, 50), "p95": _percentile(times, 95)}

    def to_dict(self) -> dict[str, Any]:
        by_outcome = {o.value: sum(1 for r in self.results if r.outcome is o) for o in Outcome}
        return {
            "summary": {
                "accuracy": self.accuracy,
                "correct": self.correct,
                "scored": len(self.scored),
                "total": len(self.results),
                "valid": self.valid,
                "by_outcome": by_outcome,
            },
            "by_difficulty": {k: {"correct": c, "scored": s} for k, (c, s) in self.breakdown("difficulty").items()},
            "by_tag": {k: {"correct": c, "scored": s} for k, (c, s) in self.breakdown("tag").items()},
            "latency_ms": self.latency(),
            "provenance": self.provenance,
            "cases": [r.to_dict() for r in self.results],
        }


def evaluate(
    suite: Suite,
    conn,
    config: AppConfig,
    client: ModelClient | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    limit: int | None = None,
) -> Run:
    """Score every case in `suite` against `conn`. Raises `PreflightError` first if
    any reference cannot be graded against."""
    dialect = config.database.dialect
    if suite.dialect is not dialect:
        raise ValueError(
            f"the test set is written in {suite.dialect.value} but the run is configured for "
            f"{dialect.value}; run each dialect against a matching database (GPT review I8)"
        )
    if max_rows < 1:
        raise ValueError(f"max_rows must be at least 1, got {max_rows}")
    cases = suite.cases[:limit] if limit is not None else suite.cases

    started = datetime.now(timezone.utc)
    database = _database_label(conn)
    references = preflight(cases, conn, dialect, max_rows)
    if client is None:
        client = get_model_client(config.model)

    results = tuple(
        _run_case(case, references[case.id], conn, config, client, max_rows) for case in cases
    )
    provenance = {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "git": _git_state(),
        "test_set": {
            "path": _relative(suite.path),
            "sha256": suite.sha256,
            "cases": len(cases),
            "limit": limit,
        },
        "schema": {"path": _relative(suite.schema_file), "sha256": suite.schema_sha256},
        "model": config.summary(),
        "database": database,
        "max_rows": max_rows,
    }
    return Run(results=results, provenance=provenance)


def preflight(cases, conn, dialect: Dialect, max_rows: int) -> dict[str, Reference]:
    """Run every reference read-only and keep its result for grading."""
    references: dict[str, Reference] = {}
    problems: list[str] = []
    for case in cases:
        try:
            ordered = is_ordered(case.expected_sql, dialect)
        except ValueError as exc:
            problems.append(f"{case.id}: reference {exc}")
            continue
        result = execute(SQLCandidate(question=case.question, sql=case.expected_sql), conn, dialect, max_rows=max_rows)
        if result.rejected:
            problems.append(f"{case.id}: reference refused: {result.reason}")
        elif result.truncated:
            problems.append(f"{case.id}: reference returns more than {max_rows} rows; raise --max-rows")
        else:
            references[case.id] = Reference(result, ordered)
    if problems:
        raise PreflightError(problems)
    return references


def _run_case(case: Case, reference: Reference, conn, config: AppConfig, client, max_rows: int) -> CaseResult:
    started = time.perf_counter()
    try:
        generation = generate_candidate(case.question, conn, config, client=client)
    except GenerationError as exc:
        outcome = Outcome.INFRA_ERROR if exc.is_infrastructure else Outcome.NO_SQL
        return CaseResult(case, outcome, str(exc), None, exc.raw_response, _ms(started), 0.0)
    generated = time.perf_counter()

    result = execute(generation.candidate, conn, config.database.dialect, max_rows=max_rows)
    timing = dict(
        sql=generation.candidate.sql,
        raw_response=generation.raw_response,
        generation_ms=(generated - started) * 1000,
        execution_ms=_ms(generated),
    )

    if result.rejected:
        # Was the query refused, or did the database go away? Ask the database
        # rather than parsing the error text (GPT review I7).
        outcome = Outcome.REJECTED if _database_alive(conn) else Outcome.INFRA_ERROR
        return CaseResult(case, outcome, result.reason, **timing)
    if result.truncated:
        return CaseResult(
            case,
            Outcome.MISMATCH,
            f"returned more than {max_rows} rows; the reference returns {len(reference.result.rows)}",
            **timing,
        )

    comparison = compare_result_sets(
        ResultSet(reference.result.columns, reference.result.rows),
        ResultSet(result.columns, result.rows),
        reference.ordered,
    )
    outcome = Outcome.MATCH if comparison.match else Outcome.MISMATCH
    return CaseResult(case, outcome, comparison.detail, **timing)


def _database_alive(conn) -> bool:
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            cursor.close()
        return True
    except Exception:
        return False
    finally:
        try:
            conn.rollback()
        except Exception:
            pass


def _ms(since: float) -> float:
    return (time.perf_counter() - since) * 1000


def _percentile(sorted_values: list[float], p: int) -> float | None:
    """Nearest-rank percentile. None for an empty list."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return round(sorted_values[rank - 1], 1)


def _database_label(conn) -> str:
    """Where the run happened, without credentials: host:port/dbname or the driver."""
    info = getattr(conn, "info", None)
    if info is not None and hasattr(info, "dbname"):
        return f"{info.host}:{info.port}/{info.dbname}"
    return type(conn).__module__


def _git_state() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# The scm_eval database
# --------------------------------------------------------------------------


def setup_eval_database(dsn: str, schema_file: Path) -> str:
    """Drop and recreate `scm_eval` from `schema_file`. Returns the schema hash.

    Refuses any DSN that does not name `scm_eval`, before connecting anywhere:
    this function drops a database, and it must only ever be that one.
    """
    import psycopg
    from psycopg import conninfo, sql

    if conninfo.conninfo_to_dict(dsn).get("dbname") != EVAL_DATABASE:
        raise ValueError(f"setup only ever (re)creates the {EVAL_DATABASE!r} database")

    text = Path(schema_file).read_text(encoding="utf-8")
    digest = hashlib.sha256(Path(schema_file).read_bytes()).hexdigest()
    name = sql.Identifier(EVAL_DATABASE)
    with psycopg.connect(conninfo.make_conninfo(dsn, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(name))
        admin.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(name, sql.Literal(_SEED_STAMP + digest))
        )
    with psycopg.connect(dsn) as conn:
        conn.execute(text)
        conn.commit()
    return digest


def open_eval_connection(dsn: str, expected_schema_sha256: str | None = None):
    """Connect to `scm_eval` and prove it is `scm_eval`, seeded from the expected file."""
    import psycopg

    conn = psycopg.connect(dsn)
    try:
        name, stamp = conn.execute(
            "SELECT current_database(), shobj_description(oid, 'pg_database') "
            "FROM pg_database WHERE datname = current_database()"
        ).fetchone()
        conn.rollback()
        if name != EVAL_DATABASE:
            raise ValueError(f"refusing to evaluate against {name!r}; only {EVAL_DATABASE!r} is allowed")
        if expected_schema_sha256 is not None and stamp != _SEED_STAMP + expected_schema_sha256:
            raise ValueError(
                f"{EVAL_DATABASE} was not seeded from the current schema file; "
                "run `python -m modules.ask.evaluate setup` first"
            )
    except BaseException:
        conn.close()
        raise
    return conn


# --------------------------------------------------------------------------
# Report and command line
# --------------------------------------------------------------------------


def print_report(run: Run) -> None:
    model = run.provenance["model"]
    # Under the mock provider the configured model name is never called, so
    # printing it would make a screenshot of this header look like a model result.
    answered_by = "mock" if model["provider"] == "mock" else model["served_model"]
    rule = "=" * 72
    print(rule)
    print(f"NL2SQL execution accuracy  |  {model['stage']} ({answered_by})  |  {run.provenance['database']}")
    print(rule)
    if model["provider"] == "mock":
        print("  MOCK MODEL: a keyword matcher, not a language model. This checks the")
        print("  pipeline; it is not a result. Do not report this number.")
    if run.accuracy is None:
        print("  accuracy      nothing could be scored")
    else:
        print(f"  accuracy      {run.correct}/{len(run.scored)} = {run.accuracy:.1%}")
    outcomes = " | ".join(f"{o.value} {sum(1 for r in run.results if r.outcome is o)}" for o in Outcome)
    print(f"  outcomes      {outcomes}")
    by_difficulty = " | ".join(f"{k} {c}/{s}" for k, (c, s) in run.breakdown("difficulty").items())
    print(f"  difficulty    {by_difficulty}")
    latency = run.latency()
    print(f"  latency       p50 {latency['p50']} ms | p95 {latency['p95']} ms (generate + execute)")
    if not run.valid:
        failed = sum(1 for r in run.results if r.outcome is Outcome.INFRA_ERROR)
        print(f"  INVALID RUN   {failed} case(s) hit an infrastructure failure. Fix it and run again;")
        print("                do not quote this number.")
    print("  per case")
    for r in run.results:
        detail = f"  {r.detail}" if r.detail and r.outcome is not Outcome.MATCH else ""
        print(f"    {r.case.id:<10} {r.outcome.value:<12}{detail[:110]}")
    git = run.provenance["git"]
    commit = (git["commit"] or "unknown")[:10] + (" (uncommitted changes)" if git["dirty"] else "")
    print(f"  commit {commit} | test set {run.provenance['test_set']['sha256'][:12]} "
          f"| schema {run.provenance['schema']['sha256'][:12]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m modules.ask.evaluate",
        description="Execution accuracy of the ask module against the scm_eval database. "
        "The DSN is read from SCM_EVAL_DSN.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help=f"drop and recreate {EVAL_DATABASE} from the schema file")
    run_cmd = commands.add_parser("run", help="score the test set and write a JSON report")
    for sub in (setup, run_cmd):
        sub.add_argument("--test-set", type=Path, default=DEFAULT_TEST_SET)
    run_cmd.add_argument("--limit", type=int, help="score only the first N cases")
    run_cmd.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    run_cmd.add_argument("--out", type=Path, help="report path (default: results/ask-eval-<time>-<stage>.json)")
    args = parser.parse_args(argv)

    dsn = os.getenv("SCM_EVAL_DSN", DEFAULT_EVAL_DSN)
    suite = load_suite(args.test_set)

    if args.command == "setup":
        digest = setup_eval_database(dsn, suite.schema_file)
        print(f"{EVAL_DATABASE} recreated from {_relative(suite.schema_file)} (sha256 {digest[:12]})")
        return 0

    config = get_config()
    try:
        conn = open_eval_connection(dsn, expected_schema_sha256=suite.schema_sha256)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        run = evaluate(suite, conn, config, max_rows=args.max_rows, limit=args.limit)
    except PreflightError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        conn.close()

    print_report(run)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or REPO_ROOT / "results" / f"ask-eval-{stamp}-{config.model.stage}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run.to_dict(), indent=2, default=str), encoding="utf-8")
    print(f"  report        {_relative(out)}")
    return 0 if run.valid else 3


if __name__ == "__main__":
    sys.exit(main())
