"""The HTTP layer — three endpoints, and a core that does not need a web server.

This module exposes the NL->SQL pipeline over HTTP so the React dashboard
(Person C) has something to call:

    GET  /health    config summary + "can I reach the database?"
    GET  /schema    the introspected schema of the configured database
    POST /query     {"question": "..."} -> generated SQL, verdict, rows

Why the file is shaped like this
--------------------------------
Every endpoint is written **twice**: once as a plain function that takes
ordinary arguments and returns an `ApiResponse` (an `(int, dict)` pair), and
once as a three-line FastAPI route that calls it. FastAPI is imported lazily,
so importing this module on a machine without FastAPI still gives you the
plain functions.

Environment note (verified 2026-08-12, and it contradicts docs/TESTING.md):
`fastapi` 0.135.1, `uvicorn` 0.41.0 and `pydantic` 2.12.5 **are** present in
`~/.local/lib/python3.12/site-packages`, so the server really can be started on
this machine. `psycopg` and `openai` are still genuinely absent, so the
PostgreSQL and vLLM paths remain blocked.

The split above is not a workaround for a missing package, and it stays whether
or not FastAPI is installed:

1.  The logic can be tested by calling a function, with no server, no port,
    no HTTP client, and no async. The self-test at the bottom of this file
    does exactly that, on the standard library alone — which is also what
    makes it runnable in CI, in a container, and on a teammate's laptop that
    has none of these packages.
2.  The same functions are reusable from anywhere else — a CLI, the eval
    harness, a notebook — without dragging a web framework in.
3.  A route with no logic in it has nothing that can break when the framework
    is upgraded, replaced, or run under a different server.

To see the FastAPI-free behaviour on this machine, run anything below with
`PYTHONNOUSERSITE=1`, which switches off the user site-packages directory
FastAPI lives in.

Status codes are decided by `status_for_result()` from the *result object*,
never from a caught exception, because `answer_question()` is documented never
to raise. A refused query and an unparseable model reply are 4xx (the request
or the model produced something we will not run); a dead database or a dead
model server is 5xx (our fault, retry later).

Read-only, always (BO-7/BO-8). Nothing here creates a table, writes a row, or
sends anything but `SELECT`s to the target database. The SQLite path is opened
with `mode=ro` so the database engine itself would refuse a write even if
something upstream went wrong.

Running it, once FastAPI is installed
-------------------------------------
    sudo apt install -y python3.12-venv        # once, if missing
    python3 -m venv .venv
    ./.venv/bin/pip install -r requirements.txt

    # from the project root, so `scm_agent` is importable:
    SCM_MODEL_PROVIDER=mock \\
    SCM_DIALECT=postgresql \\
    SCM_DB_DSN='postgresql://postgres:postgres@localhost:5434/scm_demo' \\
    ./.venv/bin/uvicorn scm_agent.app.main:app --reload --port 8080

    curl localhost:8080/health
    curl localhost:8080/schema
    curl -X POST localhost:8080/query \\
         -H 'Content-Type: application/json' \\
         -d '{"question": "How many customers are there?"}'

This module is the integration point between the two subsystems. It currently
exposes the query pipeline only; the self-healing engine will add its own routes
here later. Nothing in this module imports from `scm_agent/healing/**`, so the
two remain independent until that happens.
"""

from __future__ import annotations

import importlib.util
import re
import urllib.parse
from contextlib import contextmanager
from typing import Any, Iterator, NamedTuple

from scm_agent.pipeline.config import AppConfig, Dialect, get_config
from scm_agent.pipeline.model_client import ModelClient
from scm_agent.pipeline.nl_to_sql import (
    DEFAULT_MAX_ROWS,
    NLToSQLResult,
    answer_question,
    clear_schema_cache,
    get_schema_ddl,
)

__all__ = [
    "ApiResponse",
    "MissingDependencyError",
    "health_endpoint",
    "schema_endpoint",
    "query_endpoint",
    "run_query",
    "status_for_result",
    "open_connection",
    "close_connection",
    "database_connection",
    "sqlite_path_from_dsn",
    "create_app",
    "app",
]

# --------------------------------------------------------------------------
# Constants and the one exception this module raises
# --------------------------------------------------------------------------

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_SERVER_ERROR = 500

MAX_ROWS_CEILING = 1_000
"""Hard upper bound on `max_rows` a caller may ask for.

`DEFAULT_MAX_ROWS` (200) is the default; a client may raise it for an export,
but not without limit — an unbounded result set on a web response is a denial
of service with extra steps.
"""


class MissingDependencyError(RuntimeError):
    """A third-party package this path needs is not installed.

    Its own class, raised instead of letting `ImportError` escape, for the same
    reason `model_client.ModelClientError` exists: "No module named 'psycopg'"
    tells a student nothing about what to do next, and on this machine the fix
    is genuinely non-obvious (PEP 668 blocks `pip install`, and
    `python3.12-venv` is missing, so `python3 -m venv` yields a venv with no
    pip). The message spells the fix out.

    Raised lazily, never at module import time — importing this module must
    work with nothing but the standard library, or the self-test below could
    not run at all.
    """


class ApiResponse(NamedTuple):
    """What every core function returns: an HTTP status and a JSON-able dict.

    A `NamedTuple` rather than a framework type on purpose — it is a plain
    tuple, so a test can unpack it (`status, payload = ...`) and `payload` is
    an ordinary dict that `json.dumps` accepts. Nothing in here knows what
    FastAPI is.
    """

    status_code: int
    payload: dict[str, Any]


# --------------------------------------------------------------------------
# 1. Connection factory
# --------------------------------------------------------------------------

def sqlite_path_from_dsn(dsn: str) -> str:
    """Turn a DSN into a SQLite filename.

    Accepts the SQLAlchemy-style URL people copy out of tutorials as well as a
    bare path, because both will be typed into `SCM_DB_DSN`:

        sqlite:///:memory:      -> ":memory:"
        sqlite:///demo.db       -> "demo.db"        (relative to the cwd)
        sqlite:////tmp/demo.db  -> "/tmp/demo.db"   (four slashes = absolute)
        /tmp/demo.db            -> "/tmp/demo.db"
        ""                      -> ":memory:"

    The three-vs-four slash rule is not ours; it is what every SQLite URL in
    the wild means, and quietly getting it wrong would open the wrong file.
    """
    text = dsn.strip()
    lowered = text.lower()
    for prefix in ("sqlite+pysqlite://", "sqlite3://", "sqlite://"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            # `sqlite://` + `/path` — exactly one slash belongs to the URL
            # syntax, any further ones are part of the path itself.
            if text.startswith("/"):
                text = text[1:]
            break
    return text or ":memory:"


def open_connection(config: AppConfig | None = None, read_only: bool = True):
    """Open a DB-API 2.0 connection to the configured target database.

    The rest of the pipeline takes a connection and asks no questions about it
    (`nl_to_sql.py` imports no driver at all), so this is the only place in the
    project that maps a dialect onto a driver.

    `read_only=True` is defence in depth for BO-7. The safety layer already
    refuses to *send* a write, but on SQLite we can additionally have the
    engine refuse to *perform* one, which costs nothing and turns a hypothetical
    bug in our own code into an error instead of a data loss. A pleasant side
    effect: a missing database file becomes "unable to open database file"
    rather than SQLite silently creating an empty one and the agent reporting
    that your database has no tables.

    Raises `MissingDependencyError` when the driver is absent and
    `NotImplementedError` for a dialect nobody has wired a driver for yet.
    """
    if config is None:
        config = get_config()
    dialect = config.database.dialect
    dsn = config.database.dsn

    if dialect is Dialect.SQLITE:
        return _connect_sqlite(sqlite_path_from_dsn(dsn), read_only=read_only)
    if dialect is Dialect.POSTGRESQL:
        return _connect_postgresql(dsn, read_only=read_only)

    # MySQL and SQL Server are in scope for the NL->SQL *prompt* path
    # (docs/PROJECT_BRIEF.md) and `schema_context.py` has introspection code
    # for both, but no driver is pinned in requirements.txt yet and neither has
    # ever been run against a real server. Saying so is better than importing a
    # package that was never chosen.
    raise NotImplementedError(
        f"No driver is wired up for dialect {dialect.value!r} yet. "
        "Only 'sqlite' (stdlib sqlite3) and 'postgresql' (psycopg) can be "
        "connected to today. MySQL/SQL Server introspection exists in "
        "schema_context.py but their drivers (PyMySQL / pyodbc) are still "
        "commented out in requirements.txt and untested."
    )


def _connect_sqlite(path: str, read_only: bool):
    """`sqlite3.connect`, with a read-only URI when that is meaningful."""
    import sqlite3  # stdlib, but kept local so the import list reads honestly

    if path == ":memory:" or not read_only:
        # A read-only in-memory database would be permanently empty, so the
        # flag is ignored here. `:memory:` only ever appears in tests and demos.
        return sqlite3.connect(path)

    # SQLite's URI filenames are percent-encoded, so a path containing '?' or
    # '#' (both legal on Linux) does not silently truncate into a query string.
    return sqlite3.connect(
        f"file:{urllib.parse.quote(path)}?mode=ro", uri=True
    )


def _connect_postgresql(dsn: str, read_only: bool):
    """`psycopg.connect`, imported lazily with an actionable failure message."""
    if importlib.util.find_spec("psycopg") is None:
        raise MissingDependencyError(
            "The 'psycopg' package is required to connect to PostgreSQL but is "
            "not installed.\n"
            "  Install it into the project virtualenv:\n"
            "      sudo apt install -y python3.12-venv   # once, if missing\n"
            "      python3 -m venv .venv\n"
            "      ./.venv/bin/pip install -r requirements.txt\n"
            "  Or point the agent at a SQLite file for now:\n"
            "      export SCM_DIALECT=sqlite\n"
            "      export SCM_DB_DSN=sqlite:///scm_demo.db"
        )

    # `find_spec` first, then import: if psycopg is installed but broken, the
    # real traceback survives instead of being swallowed into our message.
    import psycopg  # noqa: PLC0415 — deliberately deferred

    conn = psycopg.connect(dsn)
    if read_only:
        # Session-level read-only: PostgreSQL itself then rejects any write,
        # whatever the rest of the stack does. Best-effort and wrapped, because
        # NO PostgreSQL SERVER HAS BEEN REACHABLE ON THIS MACHINE — this line
        # has never actually run (docs/TESTING.md).
        # A failure here must not cost us a working connection.
        try:
            conn.read_only = True
        except Exception:
            pass
    return conn


def close_connection(conn) -> None:
    """Close a connection *and* drop its entry from the DDL cache.

    The cache in `nl_to_sql.py` deliberately keeps a strong reference to the
    connection object (its docstring explains why: `id()` values get recycled,
    so a dead connection's id could otherwise be handed someone else's schema).
    That reference is only released by `clear_schema_cache(conn)`, so forgetting
    this call leaks a connection object per request.
    """
    clear_schema_cache(conn)
    try:
        conn.close()
    except Exception:
        # A failure to close is worth nothing to the caller and must not turn a
        # successful response into a 500.
        pass


@contextmanager
def database_connection(
    config: AppConfig | None = None, conn: Any | None = None
) -> Iterator[Any]:
    """Yield a connection, closing it only if we were the one who opened it.

    One connection per request, rather than one shared connection for the whole
    process. FastAPI runs synchronous route handlers in a thread pool, and
    neither `sqlite3` (which refuses cross-thread use by default) nor a single
    psycopg connection is safe to share between concurrent requests.

    The cost is that the DDL cache only pays off *within* one request — where
    `/query` still introspects once rather than twice. The fix, when it matters,
    is a connection pool, not a shared connection; that is a deliberate
    "later" (see the report notes), not an oversight.

    `conn` is the borrowed case: tests and the eval harness already hold an open
    connection, and we must not close something we did not open.
    """
    if conn is not None:
        yield conn
        return
    opened = open_connection(config)
    try:
        yield opened
    finally:
        close_connection(opened)


# --------------------------------------------------------------------------
# 2. Never leak the DSN (docs/ARCHITECTURE.md)
# --------------------------------------------------------------------------

_KEYVALUE_SECRET = re.compile(r"password\s*=\s*([^\s]+)", re.IGNORECASE)


def _dsn_secrets(dsn: str) -> list[str]:
    """Every substring of a DSN that must never reach a response body.

    Two DSN shapes are in play: the URL form
    (`postgresql://user:pw@host:5432/db`) and libpq's key/value form
    (`host=... password=pw`). Both are accepted by psycopg, so both are handled.

    `urlsplit().password` returns the *decoded* password while the DSN contains
    the encoded one, so both are collected — redacting one and missing the other
    would be worse than useless, because it would look like it worked.
    """
    secrets: list[str] = []
    if not dsn:
        return secrets

    try:
        parsed = urllib.parse.urlsplit(dsn)
    except ValueError:
        parsed = None

    if parsed is not None and parsed.netloc and "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        secrets.append(userinfo)
        if ":" in userinfo:
            secrets.append(userinfo.split(":", 1)[1])  # raw, still encoded
        try:
            if parsed.password:
                secrets.append(parsed.password)  # decoded
            if parsed.username:
                secrets.append(parsed.username)
        except ValueError:
            pass  # malformed netloc; the raw userinfo above still covers it

    secrets.extend(match.group(1) for match in _KEYVALUE_SECRET.finditer(dsn))
    # Longest first: redacting "pw" before "user:pw" would leave "user:***".
    return sorted({s for s in secrets if s}, key=len, reverse=True)


def redact(text: str, config: AppConfig) -> str:
    """Scrub the DSN and its credentials out of a message before returning it.

    Driver error messages are helpful and are *also* the most likely place for
    a connection string to escape — psycopg happily quotes the host, database
    and user back at you. `/health` is a public endpoint, so every string that
    originates from a driver goes through here first.

    Deliberately over-eager: if someone's password is literally `postgres`,
    every occurrence of that word gets starred out and the message reads oddly.
    An odd message is a much cheaper mistake than a leaked credential.
    """
    if not text:
        return text
    dsn = config.database.dsn
    cleaned = text.replace(dsn, "<dsn redacted>") if dsn else text
    for secret in _dsn_secrets(dsn):
        cleaned = cleaned.replace(secret, "***")
    return cleaned


# --------------------------------------------------------------------------
# 3. GET /health
# --------------------------------------------------------------------------

def _ping(conn) -> None:
    """`SELECT 1` — the smallest read that proves the connection works.

    Valid in all four dialects. The rollback afterwards ends the transaction
    PostgreSQL implicitly opens for any statement; leaving it open would park
    the connection in `idle in transaction`, which is precisely the anomaly the
    self-healing engine (Pillar 2) exists to hunt down. Pillar 1 generating
    Pillar 2's alerts would be an embarrassing thing to discover on stage.
    """
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    finally:
        for cleanup in (cursor.close, getattr(conn, "rollback", lambda: None)):
            try:
                cleanup()
            except Exception:
                pass


def probe_database(
    config: AppConfig, conn: Any | None = None
) -> tuple[bool, str | None]:
    """Return `(reachable, redacted_error)`. Never raises."""
    try:
        with database_connection(config, conn) as connection:
            _ping(connection)
        return True, None
    except Exception as exc:
        return False, redact(f"{type(exc).__name__}: {exc}", config)


def health_endpoint(
    config: AppConfig | None = None, conn: Any | None = None
) -> ApiResponse:
    """`GET /health` — what model are we on, and is the database alive?

    The payload is `AppConfig.summary()` (which already excludes the DSN — that
    exclusion is a tested invariant, docs/ARCHITECTURE.md) plus three fields:

        status              "ok" or "degraded"
        database_reachable  did a `SELECT 1` succeed?
        database_error      why not, with credentials scrubbed

    Always HTTP 200, even when the database is down. This endpoint answers "is
    the API process alive and what is it configured as?", and it has to keep
    answering that when the database is the thing that broke — a 503 here would
    give the dashboard nothing to display except the failure of the health
    check itself.

    At the viva this is also the honest-broker endpoint: `stage` reads
    `stage-1-mock`, `stage-2-base` or `stage-3-finetuned` straight from the
    config, so which model produced a demo is not something anyone can fudge.
    """
    if config is None:
        config = get_config()

    reachable, error = probe_database(config, conn)
    payload: dict[str, Any] = dict(config.summary())
    payload["status"] = "ok" if reachable else "degraded"
    payload["database_reachable"] = reachable
    payload["database_error"] = error
    return ApiResponse(HTTP_OK, payload)


# --------------------------------------------------------------------------
# 4. GET /schema
# --------------------------------------------------------------------------

def schema_endpoint(
    config: AppConfig | None = None,
    conn: Any | None = None,
    include_ddl: bool = False,
    refresh: bool = False,
) -> ApiResponse:
    """`GET /schema` — the structure the model is given, as JSON.

    This is BO-6 (plug-and-play) made visible: point the agent at any database
    and this endpoint reports the tables, primary keys and foreign keys it
    discovered, with no configuration and no schema changes on their side.

    `include_ddl=True` additionally returns the exact `CREATE TABLE` text that
    goes into the prompt's `{db_details}` block. Off by default because it is
    large, but it is the fastest way to answer "why did the model not know
    about that column?" — and when teammates generate fine-tuning data, it is
    how you check the training schema matches the serving one.

    `refresh=True` bypasses the DDL cache, for when a table was just added.
    Sample values are *not* collected here: `/schema` describes structure, and
    reading real rows to display a structure would be an unnecessary look at a
    customer's data.
    """
    if config is None:
        config = get_config()

    try:
        with database_connection(config, conn) as connection:
            ddl, database = get_schema_ddl(
                connection,
                config.database.dialect,
                include_samples=False,
                refresh=refresh,
            )
    except Exception as exc:
        # Unlike `/query`, there is no result object to inspect here: a failure
        # is a failure to reach or read the database, which is a server-side
        # fault, so 500 is right.
        return ApiResponse(
            HTTP_SERVER_ERROR,
            {
                "ok": False,
                "dialect": config.database.dialect.value,
                "error": redact(f"{type(exc).__name__}: {exc}", config),
            },
        )

    payload: dict[str, Any] = {"ok": True}
    payload.update(database.summary())
    payload["ddl_characters"] = len(ddl)
    if include_ddl:
        payload["ddl"] = ddl
    return ApiResponse(HTTP_OK, payload)


# --------------------------------------------------------------------------
# 5. POST /query
# --------------------------------------------------------------------------

def status_for_result(result: NLToSQLResult) -> int:
    """Map one `NLToSQLResult` onto an HTTP status code.

    Decided from the *structure of the result*, not from its error string and
    not from a caught exception. `answer_question()` is documented never to
    raise, so there is no exception to catch, and matching on message text
    would break the first time someone reworded an error.

    The split that matters:

        4xx  the request or the model produced something we will not run —
             a refused query, an unparseable reply, an empty question, or SQL
             the database rejected. Retrying the identical request cannot help;
             rephrasing might.
        5xx  our side is broken — the database is unreachable, introspection
             failed, or the model server is down. Retrying later can help.

    The debatable one is "the safety layer allowed it but execution failed".
    It is classified 400 because the overwhelmingly common cause is a
    hallucinated column or table name — a model-level outcome, not a server
    fault. The cost of that choice is that a connection dropping mid-query also
    reports 400. `/health` is the endpoint that tells you which of the two it
    was, which is part of why it exists.
    """
    if result.ok:
        return HTTP_OK
    if result.safety is not None and not result.safety.allowed:
        return HTTP_BAD_REQUEST  # blocked by the read-only gate (BO-7)
    if result.safety is not None:
        return HTTP_BAD_REQUEST  # checked and run, but the database rejected it
    if result.raw_response.strip():
        return HTTP_BAD_REQUEST  # the model answered; nothing parseable in it
    if not result.question.strip():
        return HTTP_BAD_REQUEST  # empty question
    return HTTP_SERVER_ERROR


def _failed_result(
    config: AppConfig, question: str, error: str
) -> NLToSQLResult:
    """Build a result record for a failure that happened before the pipeline.

    Constructed rather than hand-rolling a dict so the response shape is
    *identical* whatever went wrong — the dashboard reads `payload["ok"]` and
    `payload["error"]` and never has to special-case where the failure came
    from. Hand-writing the dict here would let the two shapes drift the first
    time a field is added to `NLToSQLResult`.
    """
    return NLToSQLResult(
        question=question,
        prompt="",
        raw_response="",
        sql=None,
        executed_sql=None,
        safety=None,
        error=error,
        stage=config.model.stage,
        dialect=config.database.dialect.value,
    )


def run_query(
    question: str,
    include_prompt: bool = False,
    config: AppConfig | None = None,
    conn: Any | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    include_samples: bool = True,
    client: ModelClient | None = None,
) -> ApiResponse:
    """Run one question through the pipeline and shape it as an HTTP response.

    `include_prompt=False` by default: the prompt embeds the whole schema and is
    easily 10 kB, which is worth logging once but not worth sending to a browser
    on every question. `include_prompt=True` adds the prompt and the model's raw
    reply, which is what you want when debugging a wrong answer.

    `client` lets a caller inject a `ModelClient`. In production the eval harness
    uses it to avoid opening an HTTP client per question; in the self-test below
    it is how a *forged* destructive model reply is fed through the real code
    path, since the mock model will never produce one on its own — and a test
    that cannot produce the dangerous input is not testing the guard.
    """
    if config is None:
        config = get_config()

    if not isinstance(question, str) or not question.strip():
        blank = _failed_result(config, question if isinstance(question, str) else "",
                               "empty question")
        return ApiResponse(HTTP_BAD_REQUEST, blank.to_dict(include_prompt=include_prompt))

    try:
        with database_connection(config, conn) as connection:
            result = answer_question(
                question,
                connection,
                config=config,
                include_samples=include_samples,
                max_rows=max_rows,
                client=client,
            )
    except Exception as exc:
        # Only *opening* the connection can raise here — `answer_question`
        # itself never does. A driver we cannot load or a database we cannot
        # reach is our problem, not the user's, hence 500.
        broken = _failed_result(
            config,
            question,
            f"could not open a database connection: "
            f"{redact(f'{type(exc).__name__}: {exc}', config)}",
        )
        return ApiResponse(
            HTTP_SERVER_ERROR, broken.to_dict(include_prompt=include_prompt)
        )

    return ApiResponse(
        status_for_result(result), result.to_dict(include_prompt=include_prompt)
    )


_QUERY_BODY_KEYS = frozenset({"question", "include_prompt", "max_rows"})


def query_endpoint(
    body: Any,
    config: AppConfig | None = None,
    conn: Any | None = None,
    client: ModelClient | None = None,
) -> ApiResponse:
    """`POST /query` — validate the request body, then run it.

    Body:

        {"question": "How many customers are there?",
         "include_prompt": false,      # optional, default false
         "max_rows": 200}              # optional, default DEFAULT_MAX_ROWS

    Validation lives here rather than in a Pydantic model so that it is part of
    the framework-free core: the same rules apply, and are testable, whether the
    request arrived over HTTP or from a function call.

    Unknown keys are rejected rather than ignored. A silently-ignored
    `"include_promt"` is a genuinely nasty half-hour for whoever is wiring up
    the frontend, and the cost of strictness is a clear error message.
    """
    if config is None:
        config = get_config()

    if not isinstance(body, dict):
        return ApiResponse(
            HTTP_BAD_REQUEST,
            {
                "ok": False,
                "error": "request body must be a JSON object, e.g. "
                         '{"question": "How many customers are there?"}',
            },
        )

    unknown = sorted(set(body) - _QUERY_BODY_KEYS)
    if unknown:
        return ApiResponse(
            HTTP_BAD_REQUEST,
            {
                "ok": False,
                "error": f"unknown field(s) in request body: {unknown}. "
                         f"Allowed: {sorted(_QUERY_BODY_KEYS)}",
            },
        )

    question = body.get("question")
    if not isinstance(question, str):
        return ApiResponse(
            HTTP_BAD_REQUEST,
            {"ok": False, "error": "'question' is required and must be a string"},
        )

    include_prompt = body.get("include_prompt", False)
    if not isinstance(include_prompt, bool):
        return ApiResponse(
            HTTP_BAD_REQUEST,
            {"ok": False, "error": "'include_prompt' must be true or false"},
        )

    max_rows = body.get("max_rows", DEFAULT_MAX_ROWS)
    # `bool` is a subclass of `int`, so True would otherwise sneak through as 1.
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        return ApiResponse(
            HTTP_BAD_REQUEST,
            {"ok": False, "error": "'max_rows' must be an integer"},
        )
    if not 1 <= max_rows <= MAX_ROWS_CEILING:
        return ApiResponse(
            HTTP_BAD_REQUEST,
            {
                "ok": False,
                "error": f"'max_rows' must be between 1 and {MAX_ROWS_CEILING}",
            },
        )

    return run_query(
        question,
        include_prompt=include_prompt,
        config=config,
        conn=conn,
        max_rows=max_rows,
        client=client,
    )


# --------------------------------------------------------------------------
# 6. The FastAPI wrapper — thin by design
# --------------------------------------------------------------------------

FASTAPI_AVAILABLE: bool = importlib.util.find_spec("fastapi") is not None
"""Whether FastAPI can be imported at all.

`find_spec` rather than `try: import fastapi`: if FastAPI is installed *and
broken*, we want its real traceback, not our "it is not installed" message.
"""


def _import_fastapi():
    """Import FastAPI on demand, or explain how to install it."""
    if not FASTAPI_AVAILABLE:
        raise MissingDependencyError(
            "FastAPI is not installed, so the HTTP layer cannot be built.\n"
            "  Install it:\n"
            "      sudo apt install -y python3.12-venv   # once, if missing\n"
            "      python3 -m venv .venv\n"
            "      ./.venv/bin/pip install -r requirements.txt\n"
            "  Then serve the API from the project root:\n"
            "      ./.venv/bin/uvicorn scm_agent.app.main:app --reload "
            "--port 8080\n"
            "  Until then, every endpoint is still usable as a plain function: "
            "health_endpoint(), schema_endpoint(), query_endpoint() — see the "
            "self-test in this file (python3 -m scm_agent.app.main)."
        )
    from fastapi import Body, FastAPI  # noqa: PLC0415 — deliberately deferred
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    return FastAPI, Body, JSONResponse


def create_app(config: AppConfig | None = None):
    """Build the FastAPI application.

    Every route below is a wrapper: it converts request arguments into plain
    Python, calls one core function, and turns the `(status, payload)` pair
    into a response. No logic lives in a route, because logic in a route can
    only be tested by starting a server.

    `JSONResponse` is used instead of `HTTPException` so that the body shape is
    identical on success and failure — `HTTPException` would wrap the payload in
    `{"detail": ...}` for errors only, and the dashboard would need two parsers.

    The config is resolved once, here, so a running server cannot change model
    stage halfway through a demo, and `AppConfig.from_env()`'s validation fails
    at startup rather than on the first request.
    """
    fastapi_class, body_param, json_response = _import_fastapi()
    resolved = config if config is not None else get_config()

    api = fastapi_class(
        title="SCM-OVS — Autonomous AI Database Agent",
        version="0.1.0",
        description=(
            "Ask a PostgreSQL/SQLite database questions in English. Every "
            "generated statement passes a read-only safety check before it "
            "runs (BO-7); nothing on this path can write."
        ),
    )

    @api.get("/health")
    def health():  # pragma: no cover - exercised only with FastAPI installed
        status, payload = health_endpoint(resolved)
        return json_response(status_code=status, content=payload)

    @api.get("/schema")
    def schema(include_ddl: bool = False, refresh: bool = False):  # pragma: no cover
        status, payload = schema_endpoint(
            resolved, include_ddl=include_ddl, refresh=refresh
        )
        return json_response(status_code=status, content=payload)

    @api.post("/query")
    def query(body: dict = body_param(...)):  # pragma: no cover
        status, payload = query_endpoint(body, resolved)
        return json_response(status_code=status, content=payload)

    return api


app = create_app() if FASTAPI_AVAILABLE else None
"""The ASGI application uvicorn looks for: `scm_agent.app.main:app`.

Built eagerly at import when FastAPI is installed, so a misconfigured
environment fails the moment you start the server rather than on the first
request — the same philosophy as `AppConfig.validate()`.

`None` when FastAPI is absent, so that importing this module purely for its
core functions always works. Call `create_app()` in that case if you want the
error message explaining how to install FastAPI.
"""


# --------------------------------------------------------------------------
# Self-test — every endpoint, with no server, no FastAPI, no PostgreSQL
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import os
    import sqlite3
    import tempfile
    from pathlib import Path

    from scm_agent.pipeline.model_client import FENCE_CLOSE, SQL_FENCE_OPEN

    RULE = "=" * 78
    CHECKS: list[tuple[str, bool]] = []

    def check(label: str, condition: object, detail: str = "") -> bool:
        """Record and print one assertion. Collects failures instead of
        stopping at the first, so one run shows the whole picture."""
        passed = bool(condition)
        CHECKS.append((label, passed))
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}"
              + (f"\n         {detail}" if detail else ""))
        return passed

    def show(title: str, response: ApiResponse, row_preview: int = 3) -> None:
        """Print a returned dict, trimming long row lists so it stays readable."""
        payload = dict(response.payload)
        rows = payload.get("rows")
        if isinstance(rows, list) and len(rows) > row_preview:
            payload["rows"] = rows[:row_preview] + [f"... {len(rows) - row_preview} more row(s)"]
        print(f"\n--- {title}  ->  HTTP {response.status_code}")
        print(json.dumps(payload, indent=2, default=str))

    def build_demo_database() -> sqlite3.Connection:
        """Load `db/schema.sql` into an in-memory SQLite database.

        The file targets PostgreSQL, so two translations are needed:
        `SERIAL PRIMARY KEY` -> `INTEGER PRIMARY KEY` (SQLite's auto-incrementing
        rowid alias) and `CREATE INDEX` dropped (legal in SQLite, irrelevant here).

        Loading the *real* schema file rather than a copy is the point: if a
        teammate adds a table, this test starts covering it immediately instead
        of drifting quietly out of date.
        """
        path = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
        script = path.read_text(encoding="utf-8")
        script = re.sub(r"(?i)\bSERIAL\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY", script)
        script = re.sub(r"(?is)\bCREATE\s+INDEX\b.*?;", "", script)
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(script)
        connection.commit()
        return connection

    class ForgedModelClient:
        """A model that says exactly what we tell it to.

        The mock model only ever emits SELECTs, so it cannot be used to prove
        that a write is blocked. This client forges the reply instead, which
        drives the *real* extract -> safety -> execute path with dangerous input
        — the only honest way to test the guard.
        """

        def __init__(self, reply: str) -> None:
            self.reply = reply

        def generate(self, prompt: str) -> str:
            return self.reply

    def fenced(sql: str, prose: str = "Here is the query you asked for.") -> str:
        """A reply in OmniSQL's documented shape: reasoning, then a ```sql block."""
        return f"{prose}\n\n{SQL_FENCE_OPEN}\n{sql}\n{FENCE_CLOSE}"

    # ------------------------------------------------------------------
    # Setup: a planted credential, so the /health leak check is real
    # ------------------------------------------------------------------
    PLANTED_PASSWORD = "hunter2-planted-secret"
    PLANTED_USER = "scm_admin"
    PLANTED_HOST = "db.giki.edu.pk"
    PLANTED_DSN = (
        f"postgresql://{PLANTED_USER}:{PLANTED_PASSWORD}@{PLANTED_HOST}:5434/scm_demo"
    )

    os.environ["SCM_MODEL_PROVIDER"] = "mock"
    os.environ["SCM_DB_DSN"] = PLANTED_DSN

    os.environ["SCM_DIALECT"] = "sqlite"
    sqlite_cfg = get_config()
    os.environ["SCM_DIALECT"] = "postgresql"
    postgres_cfg = get_config()

    conn = build_demo_database()
    TABLES = sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name NOT LIKE 'sqlite_%'"
        )
    )

    def table_counts() -> dict[str, int]:
        return {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in TABLES
        }

    print(RULE)
    print("SETUP")
    print(RULE)
    print(f"  stage             : {sqlite_cfg.model.stage}")
    print(f"  dialect (queries) : {sqlite_cfg.database.dialect.value}")
    print(f"  SCM_DB_DSN        : {PLANTED_DSN}   <- deliberately planted")
    print(f"  tables loaded     : {TABLES}")
    print(f"  row counts        : {table_counts()}")
    print(f"  fastapi installed : {FASTAPI_AVAILABLE}")

    # ------------------------------------------------------------------
    # 1. Connection factory
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("1. CONNECTION FACTORY")
    print(RULE)

    DSN_CASES = [
        ("sqlite:///:memory:", ":memory:"),
        ("sqlite:///demo.db", "demo.db"),
        ("sqlite:////tmp/demo.db", "/tmp/demo.db"),
        ("SQLite:///Data/Demo.db", "Data/Demo.db"),
        ("sqlite+pysqlite:///demo.db", "demo.db"),
        ("/var/lib/scm/demo.db", "/var/lib/scm/demo.db"),
        (":memory:", ":memory:"),
        ("", ":memory:"),
    ]
    for dsn, expected in DSN_CASES:
        got = sqlite_path_from_dsn(dsn)
        check(f"sqlite_path_from_dsn({dsn!r}) == {expected!r}", got == expected,
              "" if got == expected else f"got {got!r}")

    # A real file on disk, so the factory itself is exercised — not just the
    # borrowed-connection path. tempfile cleans up after itself, so this test
    # leaves nothing behind.
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "factory_probe.db"
        seed = sqlite3.connect(db_path)
        seed.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, label TEXT)")
        seed.execute("INSERT INTO demo (label) VALUES ('synthetic-row')")
        seed.commit()
        seed.close()

        os.environ["SCM_DIALECT"] = "sqlite"
        os.environ["SCM_DB_DSN"] = f"sqlite:///{db_path}"
        file_cfg = get_config()

        opened = open_connection(file_cfg)
        read_back = opened.execute("SELECT label FROM demo").fetchone()[0]
        check("open_connection() opens a real SQLite file and reads it",
              read_back == "synthetic-row", f"read {read_back!r}")

        try:
            opened.execute("INSERT INTO demo (label) VALUES ('should-never-land')")
            opened.commit()
            wrote = True
            refusal = "no error at all"
        except Exception as exc:
            wrote = False
            refusal = f"{type(exc).__name__}: {exc}"
        check("the connection is genuinely read-only (SQLite refuses a write)",
              not wrote, refusal)
        close_connection(opened)

        os.environ["SCM_DB_DSN"] = f"sqlite:///{Path(tmpdir) / 'not_there.db'}"
        try:
            missing = open_connection(get_config())
            missing.close()
            check("a missing SQLite file errors instead of being created", False,
                  "it opened an empty database instead")
        except sqlite3.OperationalError as exc:
            check("a missing SQLite file errors instead of being created", True,
                  f"{type(exc).__name__}: {exc}")

    # restore the planted DSN for the leak checks below
    os.environ["SCM_DB_DSN"] = PLANTED_DSN
    os.environ["SCM_DIALECT"] = "sqlite"

    try:
        open_connection(postgres_cfg)
        check("psycopg missing -> MissingDependencyError, not ImportError", False,
              "a connection was returned; is psycopg installed after all?")
    except MissingDependencyError as exc:
        message = str(exc)
        check("psycopg missing -> MissingDependencyError, not ImportError", True)
        check("...and the message says what to install",
              "psycopg" in message and "pip install" in message,
              message.splitlines()[0])
    except ImportError:
        check("psycopg missing -> MissingDependencyError, not ImportError", False,
              "a bare ImportError escaped")

    try:
        os.environ["SCM_DIALECT"] = "mysql"
        open_connection(get_config())
        check("an unwired dialect fails with a clear message", False)
    except NotImplementedError as exc:
        check("an unwired dialect fails with a clear message", True, str(exc)[:110])
    finally:
        os.environ["SCM_DIALECT"] = "sqlite"

    # ------------------------------------------------------------------
    # 2. GET /health — and the credential-leak check
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("2. GET /health")
    print(RULE)

    health_live = health_endpoint(sqlite_cfg, conn=conn)
    show("health_endpoint(config, conn=<live sqlite>)", health_live)

    health_down = health_endpoint(postgres_cfg)
    show("health_endpoint(config) with no reachable PostgreSQL", health_down)

    check("/health returns 200 when the database is reachable",
          health_live.status_code == HTTP_OK)
    check("/health reports database_reachable=True on a live connection",
          health_live.payload["database_reachable"] is True)
    check("/health still returns 200 when the database is down",
          health_down.status_code == HTTP_OK)
    check("/health reports database_reachable=False and says why",
          health_down.payload["database_reachable"] is False
          and bool(health_down.payload["database_error"]))
    check("/health carries the model stage (no fudging which model ran)",
          health_live.payload["stage"] == "stage-1-mock",
          f"stage={health_live.payload['stage']!r}")
    check("/health exposes every key AppConfig.summary() does",
          set(sqlite_cfg.summary()).issubset(health_live.payload))

    # A connection string is recognisable by shape — `user:password@host` —
    # which is a much better thing to search for than the word "dsn". The
    # `psycopg`-is-missing message legitimately mentions the *variable name*
    # `SCM_DB_DSN`, and a check that flagged that would be crying wolf.
    CREDENTIAL_SHAPE = re.compile(r"[^\s:/@]+:[^\s:/@]+@[^\s/]+")

    print("\n  credential-leak checks (the planted secret must appear nowhere):")
    for label, response in [("live", health_live), ("unreachable", health_down)]:
        dumped = json.dumps(response.payload)
        check(f"[{label}] the planted password is absent from /health",
              PLANTED_PASSWORD not in dumped)
        check(f"[{label}] the full DSN is absent from /health",
              PLANTED_DSN not in dumped)
        check(f"[{label}] the database host is absent from /health",
              PLANTED_HOST not in dumped)
        check(f"[{label}] the database username is absent from /health",
              PLANTED_USER not in dumped)
        found = CREDENTIAL_SHAPE.search(dumped)
        check(f"[{label}] nothing shaped like 'user:password@host' appears",
              found is None, "" if found is None else f"found {found.group(0)!r}")

    # ------------------------------------------------------------------
    # 3. GET /schema
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("3. GET /schema")
    print(RULE)

    schema_response = schema_endpoint(sqlite_cfg, conn=conn)
    show("schema_endpoint(config, conn)", schema_response)

    returned_tables = sorted(schema_response.payload.get("tables", {}))
    EXPECTED_TABLES = ["customers", "order_items", "orders", "products"]
    check("/schema returns 200", schema_response.status_code == HTTP_OK)
    check(f"/schema lists exactly the 4 expected tables {EXPECTED_TABLES}",
          returned_tables == EXPECTED_TABLES, f"got {returned_tables}")
    check("/schema reports the foreign keys the model needs for joins",
          schema_response.payload["tables"]["order_items"]["foreign_keys"] != [],
          str(schema_response.payload["tables"]["order_items"]["foreign_keys"]))
    check("/schema omits the DDL text by default",
          "ddl" not in schema_response.payload
          and schema_response.payload["ddl_characters"] > 0,
          f"ddl_characters={schema_response.payload['ddl_characters']}")

    with_ddl = schema_endpoint(sqlite_cfg, conn=conn, include_ddl=True)
    check("/schema?include_ddl=true returns the CREATE TABLE text",
          'CREATE TABLE "customers"' in str(with_ddl.payload.get("ddl", "")))

    broken_schema = schema_endpoint(postgres_cfg)
    check("/schema returns 500 when the database cannot be reached",
          broken_schema.status_code == HTTP_SERVER_ERROR,
          str(broken_schema.payload.get("error", ""))[:90])
    check("...and that error leaks no credential",
          PLANTED_PASSWORD not in json.dumps(broken_schema.payload))

    # ------------------------------------------------------------------
    # 4. POST /query — the happy path
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("4. POST /query — normal questions")
    print(RULE)

    counted = query_endpoint({"question": "How many customers are there?"},
                             sqlite_cfg, conn=conn)
    show("query_endpoint({'question': 'How many customers are there?'})", counted)

    joined = query_endpoint({"question": "How many orders does each customer have?"},
                            sqlite_cfg, conn=conn)
    show("query_endpoint({'question': 'How many orders does each customer have?'})",
         joined)

    check("a normal question returns 200", counted.status_code == HTTP_OK)
    check("...with ok=True and at least one row",
          counted.payload["ok"] is True and counted.payload["row_count"] >= 1,
          f"rows={counted.payload['rows']}")
    check("...and the count matches the 9 seeded customers",
          counted.payload["rows"] == [[9]], str(counted.payload["rows"]))
    check("...and the safety verdict travelled with the result (audit, BO-8)",
          counted.payload["safety"]["allowed"] is True)
    check("...and executed_sql is the safety layer's normalized SQL",
          counted.payload["executed_sql"] is not None
          and ";" not in counted.payload["executed_sql"],
          repr(counted.payload["executed_sql"]))
    check("a 4-table-shaped question also returns 200 with rows",
          joined.status_code == HTTP_OK and joined.payload["row_count"] == 9,
          f"row_count={joined.payload['row_count']}")
    check("include_prompt defaults to false (the response stays small)",
          "prompt" not in counted.payload and "raw_response" not in counted.payload,
          f"payload is {len(json.dumps(counted.payload))} bytes")

    verbose = query_endpoint(
        {"question": "How many customers are there?", "include_prompt": True},
        sqlite_cfg, conn=conn,
    )
    check("include_prompt=true adds the prompt and the raw model reply",
          "prompt" in verbose.payload and "raw_response" in verbose.payload,
          f"payload grows to {len(json.dumps(verbose.payload))} bytes")
    check("...and the prompt really is the OmniSQL template with our schema in it",
          "Database Engine:" in verbose.payload["prompt"]
          and 'CREATE TABLE "customers"' in verbose.payload["prompt"])

    # ------------------------------------------------------------------
    # 5. POST /query — a write must be refused with 400, and must not run
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("5. POST /query — a destructive model reply is refused (400, not 500)")
    print(RULE)

    before = table_counts()
    print(f"  row counts before : {before}")

    FORGERIES = [
        ("plain DROP", fenced("DROP TABLE orders;",
                              "The user asked me to tidy up, so I will remove the table.")),
        ("stacked statement after a SELECT",
         fenced("SELECT * FROM customers; DROP TABLE customers;",
                "First I read the table, then I clean it.")),
        ("data-modifying CTE (starts with WITH, still writes)",
         fenced("WITH gone AS (DELETE FROM order_items RETURNING *)\n"
                "SELECT COUNT(*) FROM gone;", "A CTE lets me do both at once.")),
        ("payload hidden behind a line comment",
         fenced("SELECT 1 --\nDROP TABLE products;", "Here you go.")),
    ]

    for label, reply in FORGERIES:
        response = query_endpoint(
            {"question": "please clean up the database"},
            sqlite_cfg, conn=conn, client=ForgedModelClient(reply),
        )
        payload = response.payload
        print(f"\n  {label}")
        print(f"    status       : HTTP {response.status_code}")
        print(f"    extracted SQL: {(payload['sql'] or '').splitlines()[0]!r} ...")
        print(f"    safety       : allowed={payload['safety']['allowed']} "
              f"violations={payload['safety']['violations']}")
        print(f"    executed_sql : {payload['executed_sql']}")
        print(f"    error        : {str(payload['error'])[:100]}")
        check(f"[{label}] returns 400, not 500",
              response.status_code == HTTP_BAD_REQUEST)
        check(f"[{label}] the safety layer refused it",
              payload["safety"] is not None and payload["safety"]["allowed"] is False)
        check(f"[{label}] nothing was executed",
              payload["executed_sql"] is None and payload["rows"] == []
              and payload["ok"] is False)

    after = table_counts()
    still_there = sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name NOT LIKE 'sqlite_%'"
        )
    )
    print(f"\n  row counts after  : {after}")
    check("every row count is unchanged — no write got through",
          after == before, f"before={before} after={after}")
    check("every table is still present — nothing was dropped",
          still_there == TABLES, f"{still_there}")

    # ------------------------------------------------------------------
    # 6. POST /query — the other 4xx/5xx paths
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("6. POST /query — bad input, bad model output, broken server")
    print(RULE)

    unparseable = query_endpoint(
        {"question": "what is the meaning of life?"},
        sqlite_cfg, conn=conn,
        client=ForgedModelClient("I am sorry, I cannot answer that question."),
    )
    show("model replies with prose and no SQL at all", unparseable)
    check("unparseable model output -> 400",
          unparseable.status_code == HTTP_BAD_REQUEST)
    check("...and the error names the problem",
          "no SQL found" in str(unparseable.payload["error"]),
          str(unparseable.payload["error"])[:100])

    invalid_sql = query_endpoint(
        {"question": "how many widgets are there?"},
        sqlite_cfg, conn=conn,
        client=ForgedModelClient(fenced("SELECT COUNT(*) FROM widgets;")),
    )
    check("a SELECT the database rejects (hallucinated table) -> 400",
          invalid_sql.status_code == HTTP_BAD_REQUEST,
          str(invalid_sql.payload["error"])[:100])

    blank = query_endpoint({"question": "   "}, sqlite_cfg, conn=conn)
    check("an empty question -> 400 with the standard result shape",
          blank.status_code == HTTP_BAD_REQUEST
          and blank.payload["error"] == "empty question"
          and blank.payload["ok"] is False)

    BAD_BODIES = [
        ("not an object at all", "How many customers are there?"),
        ("missing 'question'", {"include_prompt": True}),
        ("'question' is not a string", {"question": 42}),
        ("'include_prompt' is not a bool", {"question": "hi", "include_prompt": "yes"}),
        ("'max_rows' is not an integer", {"question": "hi", "max_rows": "200"}),
        ("'max_rows' out of range", {"question": "hi", "max_rows": 10_000}),
        ("typo'd field name", {"question": "hi", "include_promt": True}),
    ]
    print()
    for label, body in BAD_BODIES:
        response = query_endpoint(body, sqlite_cfg, conn=conn)
        check(f"malformed body ({label}) -> 400",
              response.status_code == HTTP_BAD_REQUEST,
              str(response.payload.get("error"))[:96])

    server_fault = query_endpoint({"question": "How many customers are there?"},
                                  postgres_cfg)
    show("the database cannot be reached at all", server_fault)
    check("an unreachable database -> 500, not 400",
          server_fault.status_code == HTTP_SERVER_ERROR)
    check("...and that response leaks no credential either",
          PLANTED_PASSWORD not in json.dumps(server_fault.payload)
          and PLANTED_DSN not in json.dumps(server_fault.payload))

    # ------------------------------------------------------------------
    # 7. The module works without FastAPI
    # ------------------------------------------------------------------
    print()
    print(RULE)
    print("7. THE FRAMEWORK IS OPTIONAL — the core works with or without FastAPI")
    print(RULE)

    # True in both worlds: the lazy import never binds a module-level name, so
    # nothing above this line could have depended on FastAPI even by accident.
    check("no module-level name binds FastAPI (the import really is lazy)",
          "fastapi" not in globals() and "FastAPI" not in globals())
    check("every core function above ran on the standard library alone",
          all(name in globals() for name in
              ("health_endpoint", "schema_endpoint", "query_endpoint", "run_query")))

    if FASTAPI_AVAILABLE:
        check("FastAPI is installed here, so `app` is a real ASGI application",
              app is not None, f"app = {type(app).__name__}")
        registered = {
            getattr(route, "path", None): sorted(getattr(route, "methods", None) or [])
            for route in getattr(app, "routes", [])
        }
        for path, method in (("/health", "GET"), ("/schema", "GET"), ("/query", "POST")):
            check(f"{method} {path} is registered on the app",
                  method in registered.get(path, []),
                  f"registered: {sorted(p for p in registered if p)}")

        # The FastAPI-free claim cannot be tested in this process — the package
        # is importable here. So prove it in a child process with the user
        # site-packages directory (where FastAPI lives on this machine) turned
        # off, which is the closest thing to a machine that never had it.
        import subprocess
        import sys

        probe = subprocess.run(
            [sys.executable, "-c",
             "import scm_agent.app.main as m; "
             "print(m.FASTAPI_AVAILABLE, m.app, callable(m.health_endpoint))"],
            cwd=str(Path(__file__).resolve().parents[2]),
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
            capture_output=True, text=True,
        )
        output = probe.stdout.strip()
        check("with FastAPI hidden, the module still imports cleanly",
              probe.returncode == 0,
              f"exit={probe.returncode} {probe.stderr.strip()[:200]}")
        check("...and reports `app is None` while the core stays callable",
              output == "False None True", f"child printed {output!r}")
    else:
        check("FastAPI is absent, so module-level `app` is None", app is None)
        try:
            create_app()
            check("create_app() raises a clear MissingDependencyError", False,
                  "it returned an app, which cannot be right")
        except MissingDependencyError as exc:
            message = str(exc)
            check("create_app() raises a clear MissingDependencyError", True)
            check("...and the message explains how to install and how to run it",
                  "pip install" in message and "uvicorn" in message)
            print("\n  the message a developer would see:")
            for line in message.splitlines():
                print(f"    {line}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print(RULE)
    failures = [label for label, passed in CHECKS if not passed]
    print(f"SUMMARY: {len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    print(RULE)
    for label in failures:
        print(f"  FAILED: {label}")

    close_connection(conn)
    for key in ("SCM_MODEL_PROVIDER", "SCM_DIALECT", "SCM_DB_DSN"):
        os.environ.pop(key, None)

    if failures:
        raise SystemExit(1)
    print("ALL CHECKS PASSED")
