"""STAND-IN for Raneem's validator and executor. Delete when `modules/execute` lands.

Only as clever as it must be to keep the database safe and evaluation honest:

1. Statement check: the existing lexical gate in `scm_agent/pipeline/safety.py`.
   Not the sqlglot validator CLAUDE.md asks for; building that is Raneem's job.
2. A session that cannot write: a read-only transaction with a statement
   timeout on PostgreSQL, `query_only` on SQLite. This is the layer that
   actually holds, because `SELECT nextval(...)` passes any statement check
   (GPT review I1). If the session cannot be made read-only, nothing runs.
3. Row cap, reported as `truncated`.

Every failure comes back as a rejection with a reason, because that reason is
what the orchestrator feeds back to the model on retry.
"""

from __future__ import annotations

from contracts import ExecutionResult, SQLCandidate
from scm_agent.pipeline.config import Dialect
from scm_agent.pipeline.nl_to_sql import DEFAULT_MAX_ROWS
from scm_agent.pipeline.nl_to_sql import _execute as _fetch
from scm_agent.pipeline.safety import check_sql

DEFAULT_STATEMENT_TIMEOUT_MS = 5000


class _NotReadOnly(RuntimeError):
    """The session could not be made read-only, so the query must not run."""


def execute(
    candidate: SQLCandidate,
    conn,
    dialect: Dialect,
    max_rows: int = DEFAULT_MAX_ROWS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> ExecutionResult:
    if max_rows < 1:
        # `fetchmany(0)` reads everything on some drivers (GPT review M5).
        raise ValueError(f"max_rows must be at least 1, got {max_rows}")
    if statement_timeout_ms < 1:
        # PostgreSQL reads 0 as "no timeout".
        raise ValueError(f"statement_timeout_ms must be at least 1, got {statement_timeout_ms}")

    verdict = check_sql(candidate.sql, dialect)
    if not verdict.allowed:
        return ExecutionResult(rejected=True, reason=f"not a read-only query: {verdict.reason}")

    try:
        _enter_read_only(conn, dialect, statement_timeout_ms)
        # `_fetch` reads one row past the cap to know whether it truncated, and
        # always rolls back, so the session is never left idle in transaction.
        columns, rows, truncated = _fetch(conn, verdict.normalized_sql, max_rows)
    except _NotReadOnly as exc:
        _rollback(conn)
        return ExecutionResult(rejected=True, reason=f"refusing to run: {exc}")
    except Exception as exc:
        _rollback(conn)
        return ExecutionResult(rejected=True, reason=_describe(exc))

    return ExecutionResult(columns=columns, rows=rows, truncated=truncated)


def _enter_read_only(conn, dialect: Dialect, statement_timeout_ms: int) -> None:
    """Make the next statement on `conn` unable to write, and prove it."""
    try:
        if dialect is Dialect.POSTGRESQL:
            if getattr(conn, "autocommit", False):
                raise _NotReadOnly(
                    "connection is in autocommit mode, so no read-only transaction can be held"
                )
            conn.rollback()  # start on a clean transaction boundary
            state = _run(
                conn,
                "SET TRANSACTION READ ONLY",
                f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}",
                "SHOW transaction_read_only",
            )
            if state != "on":
                raise _NotReadOnly(f"transaction_read_only is {state!r}")
        elif dialect is Dialect.SQLITE:
            state = _run(conn, "PRAGMA query_only = ON", "PRAGMA query_only")
            if state != 1:
                raise _NotReadOnly("SQLite query_only could not be enabled")
        else:
            raise _NotReadOnly(f"the stand-in executor has no read-only mode for {dialect.value}")
    except _NotReadOnly:
        raise
    except Exception as exc:
        raise _NotReadOnly(f"could not make the session read-only: {_describe(exc)}") from exc


def _run(conn, *statements: str):
    """Execute `statements` in order and return the single value of the last one."""
    cursor = conn.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
        (value,) = cursor.fetchone()
        return value
    finally:
        cursor.close()


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass  # best effort; the rejection is already the answer


def _describe(exc: Exception) -> str:
    """First line of the database's own message: specific enough to retry on."""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {message}"[:300]
