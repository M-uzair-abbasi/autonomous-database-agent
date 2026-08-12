"""Explicitly guarded anomaly demos, isolated from the normal engine."""

from __future__ import annotations

import re
import time
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from .config import HealingConfig


DEMO_PREFIX = "scm_ovs_demo_"
BLOAT_TABLE = f"{DEMO_PREFIX}bloat"
MAX_BLOAT_ROWS = 1_000_000
MAX_PRESSURE_CONNECTIONS = 32
MAX_HOLD_SECONDS = 24 * 60 * 60

_BLOAT_APPLICATION = f"{DEMO_PREFIX}bloat_setup"
_IDLE_APPLICATION = f"{DEMO_PREFIX}idle_transaction"
_PRESSURE_APPLICATION = f"{DEMO_PREFIX}pressure"
_CLEANUP_APPLICATION = f"{DEMO_PREFIX}cleanup"
_CURRENT_DATABASE_SQL = "SELECT pg_catalog.current_database()"
_SIMPLE_DATABASE_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_CONNINFO_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_CREATE_BLOAT_SQL = f"""
CREATE TABLE IF NOT EXISTS \"{BLOAT_TABLE}\" (
    id bigint PRIMARY KEY,
    payload text NOT NULL,
    touched_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
)
"""
_DISABLE_BLOAT_AUTOVACUUM_SQL = (
    f'ALTER TABLE "{BLOAT_TABLE}" SET (autovacuum_enabled = false)'
)
_TRUNCATE_BLOAT_SQL = f'TRUNCATE TABLE "{BLOAT_TABLE}"'
_INSERT_BLOAT_SQL = f"""
INSERT INTO \"{BLOAT_TABLE}\" (id, payload)
SELECT item, pg_catalog.repeat('x', 512)
FROM pg_catalog.generate_series(1, %s) AS item
"""
_UPDATE_BLOAT_SQL = f"""
UPDATE \"{BLOAT_TABLE}\"
SET payload = payload || 'y', touched_at = pg_catalog.clock_timestamp()
"""
# Uses mod() rather than the `%` operator on purpose. Psycopg only unescapes
# `%%` when parameters are passed, and this statement is executed with none
# (see setup_bloat below) — so `%%` reached the server literally and failed with
# "operator does not exist: bigint %% integer", aborting `demo bloat setup`
# after the earlier statements had already committed. A single `%` would work
# today but only while this call stays parameterless; mod() is correct either
# way and cannot regress if a parameter is added later.
_DELETE_BLOAT_SQL = f'DELETE FROM "{BLOAT_TABLE}" WHERE pg_catalog.mod(id, 4) <> 0'
_DROP_BLOAT_SQL = f'DROP TABLE IF EXISTS "{BLOAT_TABLE}"'

_TERMINATE_EXACT_TAG_SQL = """
SELECT pg_catalog.pg_terminate_backend(activity.pid)
FROM pg_catalog.pg_stat_activity AS activity
WHERE activity.pid <> pg_catalog.pg_backend_pid()
  AND activity.backend_type = 'client backend'
  AND activity.application_name = %s
"""
_TERMINATE_DEMO_TAGS_SQL = """
SELECT pg_catalog.pg_terminate_backend(activity.pid)
FROM pg_catalog.pg_stat_activity AS activity
WHERE activity.pid <> pg_catalog.pg_backend_pid()
  AND activity.backend_type = 'client backend'
  AND activity.application_name LIKE %s ESCAPE '\\'
"""


class DemoGuardError(RuntimeError):
    """Raised before I/O when a demo target is not explicitly disposable."""


class _Cursor(Protocol):
    def execute(self, sql: object, parameters: object = None) -> object: ...
    def fetchone(self) -> object: ...
    def __enter__(self) -> "_Cursor": ...
    def __exit__(self, *args: object) -> object: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...
    def rollback(self) -> object: ...
    def close(self) -> object: ...
    def __enter__(self) -> "_Connection": ...
    def __exit__(self, *args: object) -> object: ...


def _database_name(dsn: str) -> str | None:
    try:
        parsed = urlsplit(dsn)
    except ValueError:
        return None
    if parsed.scheme.casefold() in ("postgres", "postgresql"):
        if parsed.fragment or "%" in parsed.path or "%" in parsed.query:
            return None
        if not parsed.path.startswith("/") or parsed.path.count("/") != 1:
            return None
        name = parsed.path[1:]
        if _SIMPLE_DATABASE_NAME.fullmatch(name) is None:
            return None
        for field in parsed.query.split("&") if parsed.query else ():
            key = field.split("=", 1)[0].casefold()
            if key in ("dbname", "database"):
                return None
        return name
    if parsed.scheme:
        return None
    if any(character in dsn for character in ("'", '"', "\\")):
        return None
    values: dict[str, str] = {}
    database_specs: list[str] = []
    for field in dsn.split():
        if field.count("=") != 1:
            return None
        key, value = field.split("=", 1)
        normalized_key = key.casefold()
        if (
            _CONNINFO_KEY.fullmatch(key) is None
            or not value
            or normalized_key in values
        ):
            return None
        values[normalized_key] = value
        if normalized_key in ("dbname", "database"):
            database_specs.append(value)
    if len(database_specs) != 1:
        return None
    name = database_specs[0]
    return name if _SIMPLE_DATABASE_NAME.fullmatch(name) is not None else None


def ensure_disposable_target(dsn: str, environ: Mapping[str, str]) -> None:
    """Fail closed unless the database is named disposable or override is current."""
    database_name = _database_name(dsn)
    explicitly_allowed = environ.get("SCM_HEALING_ALLOW_DEMO") == "1"
    if database_name is None or (
        not explicitly_allowed and not database_name.startswith(DEMO_PREFIX)
    ):
        raise DemoGuardError(
            "demo refused: use a database named scm_ovs_demo_* or set SCM_HEALING_ALLOW_DEMO=1"
        )


def _default_wait(seconds: float | None) -> None:
    if seconds is not None:
        time.sleep(seconds)
        return
    while True:
        time.sleep(3600)


class DemoRunner:
    """Create and clean only prefixed tables and tagged demo sessions."""

    def __init__(
        self,
        config: HealingConfig,
        *,
        environ: Mapping[str, str],
        connect: Callable[..., _Connection] | None = None,
        wait: Callable[[float | None], object] = _default_wait,
    ) -> None:
        self._config = config
        self._environ = dict(environ)
        self._connector = connect
        self._wait = wait

    def _guard(self) -> None:
        ensure_disposable_target(self._config.database_dsn, self._environ)

    def _connect(self, application_name: str, *, autocommit: bool) -> _Connection:
        connector = self._connector
        if connector is None:
            try:
                import psycopg  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL live demos require the project Psycopg dependency"
                ) from exc
            connector = psycopg.connect
        connection = connector(
            self._config.database_dsn,
            application_name=application_name,
            autocommit=autocommit,
            connect_timeout=5,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(_CURRENT_DATABASE_SQL)
                row = cursor.fetchone()
            if (
                not isinstance(row, (tuple, list))
                or len(row) != 1
                or not isinstance(row[0], str)
            ):
                raise DemoGuardError("demo refused: effective database could not be verified")
            effective = row[0]
            if not effective.startswith(DEMO_PREFIX):
                # SCM_HEALING_ALLOW_DEMO=1 previously disabled this runtime check
                # as well as the static one in ensure_disposable_target, so a
                # single exported variable removed both guards at once. It now
                # only permits the database the DSN actually named: if the
                # connection lands somewhere else — a libpq service file, a
                # pooler rewrite, an inherited PGDATABASE — this still refuses.
                claimed = _database_name(self._config.database_dsn)
                overridden = self._environ.get("SCM_HEALING_ALLOW_DEMO") == "1"
                if not (overridden and claimed is not None and effective == claimed):
                    raise DemoGuardError(
                        "demo refused: effective database is not disposable"
                    )
            if not autocommit:
                connection.rollback()
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _validate_duration(seconds: float | None) -> None:
        if seconds is None:
            return
        if isinstance(seconds, bool) or not 0 <= seconds <= MAX_HOLD_SECONDS:
            raise ValueError("seconds must be between zero and 86400")

    def setup_bloat(self, *, rows: int) -> dict[str, object]:
        if isinstance(rows, bool) or not 1 <= rows <= MAX_BLOAT_ROWS:
            raise ValueError(f"rows must be between 1 and {MAX_BLOAT_ROWS}")
        self._guard()
        with self._connect(_BLOAT_APPLICATION, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_CREATE_BLOAT_SQL)
                cursor.execute(_DISABLE_BLOAT_AUTOVACUUM_SQL)
                cursor.execute(_TRUNCATE_BLOAT_SQL)
                cursor.execute(_INSERT_BLOAT_SQL, (rows,))
                cursor.execute(_UPDATE_BLOAT_SQL)
                cursor.execute(_DELETE_BLOAT_SQL)
        return {"scenario": "bloat", "status": "ready", "rows_requested": rows}

    def hold_idle(self, *, seconds: float | None) -> dict[str, object]:
        self._validate_duration(seconds)
        self._guard()
        connection = self._connect(_IDLE_APPLICATION, autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SELECT 1")
            self._wait(seconds)
            return {"scenario": "idle", "status": "held"}
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def hold_pressure(self, *, connections: int, seconds: float | None) -> dict[str, object]:
        if (
            isinstance(connections, bool)
            or not 1 <= connections <= MAX_PRESSURE_CONNECTIONS
        ):
            raise ValueError(
                f"connections must be between 1 and {MAX_PRESSURE_CONNECTIONS}"
            )
        self._validate_duration(seconds)
        self._guard()
        opened: list[_Connection] = []
        try:
            for _ in range(connections):
                connection = self._connect(_PRESSURE_APPLICATION, autocommit=True)
                opened.append(connection)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
            self._wait(seconds)
            return {
                "scenario": "pressure",
                "status": "held",
                "connections_requested": connections,
            }
        finally:
            for connection in reversed(opened):
                connection.close()

    def cleanup_bloat(self) -> dict[str, object]:
        self._guard()
        with self._connect(_CLEANUP_APPLICATION, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_DROP_BLOAT_SQL)
        return {"scenario": "bloat", "status": "cleaned"}

    def _cleanup_tag(self, application_name: str, scenario: str) -> dict[str, object]:
        self._guard()
        with self._connect(_CLEANUP_APPLICATION, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_TERMINATE_EXACT_TAG_SQL, (application_name,))
        return {"scenario": scenario, "status": "cleaned"}

    def cleanup_idle(self) -> dict[str, object]:
        return self._cleanup_tag(_IDLE_APPLICATION, "idle")

    def cleanup_pressure(self) -> dict[str, object]:
        return self._cleanup_tag(_PRESSURE_APPLICATION, "pressure")

    def cleanup_all(self) -> dict[str, object]:
        self._guard()
        escaped_prefix = DEMO_PREFIX.replace("_", r"\_")
        with self._connect(_CLEANUP_APPLICATION, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_TERMINATE_DEMO_TAGS_SQL, (f"{escaped_prefix}%",))
                cursor.execute(_DROP_BLOAT_SQL)
        return {"scenario": "all", "status": "cleaned"}

    def dispatch(
        self,
        scenario: str,
        operation: str,
        *,
        rows: int | None = None,
        connections: int | None = None,
        seconds: float | None = None,
    ) -> dict[str, object]:
        if scenario == "bloat" and operation == "setup":
            return self.setup_bloat(rows=20_000 if rows is None else rows)
        if scenario == "bloat" and operation == "cleanup":
            return self.cleanup_bloat()
        if scenario == "idle" and operation == "hold":
            return self.hold_idle(seconds=seconds)
        if scenario == "idle" and operation == "cleanup":
            return self.cleanup_idle()
        if scenario == "pressure" and operation == "hold":
            return self.hold_pressure(
                connections=10 if connections is None else connections,
                seconds=seconds,
            )
        if scenario == "pressure" and operation == "cleanup":
            return self.cleanup_pressure()
        if scenario == "cleanup" and operation == "cleanup":
            return self.cleanup_all()
        raise ValueError("unsupported demo operation")


__all__ = [
    "BLOAT_TABLE",
    "DEMO_PREFIX",
    "MAX_BLOAT_ROWS",
    "MAX_PRESSURE_CONNECTIONS",
    "DemoGuardError",
    "DemoRunner",
    "ensure_disposable_target",
]
