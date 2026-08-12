"""Lazy PostgreSQL monitoring and safety-gated healing actions.

The module is importable without a PostgreSQL driver.  All statements are
module-owned constants: callers can supply typed plans, never SQL text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
from typing import Callable, Mapping, Protocol, Sequence

from .actions import build_reindex_command, build_vacuum_command
from .config import HealingConfig
from .models import (
    ActionKind,
    ActionPlan,
    ActionResult,
    ActionStatus,
    ActivitySample,
    BackendTarget,
    CollectionError,
    ConnectionSample,
    HealthSnapshot,
    IndexTarget,
    IndexStatsSample,
    Mode,
    TableTarget,
    TableStatsSample,
)


_INSTALL_GUIDANCE = 'pip install "psycopg[binary]"'
_MONITOR_APPLICATION = "scm-ovs/monitor"
_HEALER_APPLICATION = "scm-ovs/healer"


class PsycopgUnavailableError(RuntimeError):
    """Raised only when a live PostgreSQL operation needs a missing driver."""


@dataclass(frozen=True)
class PlanAuthorization:
    """A capability decision paired with the exact immutable authorized plan."""

    decision: "CapabilityDecision"
    plan: ActionPlan


def _driver_is_available() -> bool:
    return importlib.util.find_spec("psycopg") is not None


def _load_psycopg() -> object:
    """Load psycopg at the live-I/O boundary, never during module import."""
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PsycopgUnavailableError(
            f"PostgreSQL live operations require psycopg; install it with {_INSTALL_GUIDANCE}"
        ) from exc
    return psycopg


ACTIVITY_SQL = """
/* scm-ovs:activity */
SELECT
    pg_catalog.clock_timestamp() AS observed_at,
    activity.datid AS database_oid,
    activity.datname AS database_name,
    activity.pid,
    activity.backend_start,
    activity.usename AS role_name,
    activity.application_name,
    activity.backend_type,
    activity.state,
    activity.state_change,
    activity.xact_start AS transaction_start,
    activity.query_start,
    activity.wait_event_type,
    activity.wait_event,
    (activity.pid = pg_catalog.pg_backend_pid()) AS is_self
FROM pg_catalog.pg_stat_activity AS activity
WHERE activity.datid = (
    SELECT database.oid
    FROM pg_catalog.pg_database AS database
    WHERE database.datname = pg_catalog.current_database()
)
  AND activity.pid <> pg_catalog.pg_backend_pid()
  AND activity.backend_type = 'client backend'
"""


TABLE_STATS_SQL = """
/* scm-ovs:tables */
SELECT
    pg_catalog.clock_timestamp() AS observed_at,
    database.oid AS database_oid,
    stats.relid AS relation_oid,
    stats.schemaname AS schema_name,
    stats.relname AS relation_name,
    stats.n_live_tup,
    stats.n_dead_tup,
    stats.last_autovacuum,
    stats.last_vacuum,
    stats.vacuum_count,
    stats.autovacuum_count,
    pg_catalog.pg_relation_size(stats.relid) AS relation_size_bytes
FROM pg_catalog.pg_stat_user_tables AS stats
JOIN pg_catalog.pg_database AS database
  ON database.datname = pg_catalog.current_database()
"""


INDEX_STATS_SQL = """
/* scm-ovs:indexes */
SELECT
    pg_catalog.clock_timestamp() AS observed_at,
    database.oid AS database_oid,
    stats.relid AS table_oid,
    stats.indexrelid AS index_oid,
    stats.schemaname AS schema_name,
    stats.relname AS table_name,
    stats.indexrelname AS index_name,
    pg_catalog.pg_relation_size(stats.indexrelid) AS index_size_bytes,
    stats.idx_scan,
    index_catalog.indisvalid AS is_valid,
    index_catalog.indisready AS is_ready,
    index_catalog.indislive AS is_live,
    index_catalog.indisunique AS is_unique,
    index_catalog.indisprimary AS is_primary,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint AS constraint_catalog
        WHERE constraint_catalog.conindid = stats.indexrelid
    ) AS is_constraint,
    database_stats.stats_reset,
    pg_catalog.pg_postmaster_start_time() AS server_started
FROM pg_catalog.pg_stat_user_indexes AS stats
JOIN pg_catalog.pg_index AS index_catalog
  ON index_catalog.indexrelid = stats.indexrelid
JOIN pg_catalog.pg_database AS database
  ON database.datname = pg_catalog.current_database()
LEFT JOIN pg_catalog.pg_stat_database AS database_stats
  ON database_stats.datid = database.oid
"""


SATURATION_SQL = """
/* scm-ovs:saturation */
SELECT
    pg_catalog.clock_timestamp() AS observed_at,
    pg_catalog.count(*) FILTER (
        WHERE activity.backend_type = 'client backend'
    )::bigint AS client_backend_count,
    pg_catalog.current_setting('max_connections')::integer AS maximum_connections,
    (
        pg_catalog.current_setting('superuser_reserved_connections')::integer
        + COALESCE(
            NULLIF(pg_catalog.current_setting('reserved_connections', true), '')::integer,
            0
        )
    ) AS reserved_connections
FROM pg_catalog.pg_stat_activity AS activity
"""


_SET_STATEMENT_TIMEOUT_SQL = """
/* scm-ovs:set-statement-timeout */
SELECT pg_catalog.set_config('statement_timeout', %s, false)
"""

_SET_LOCK_TIMEOUT_SQL = """
/* scm-ovs:set-lock-timeout */
SELECT pg_catalog.set_config('lock_timeout', %s, false)
"""

SERVER_FACTS_SQL = """
/* scm-ovs:server-facts */
SELECT
    pg_catalog.current_setting('server_version_num')::integer AS server_version_num,
    pg_catalog.version() AS server_version,
    pg_catalog.current_database() AS database_name,
    database.oid AS database_oid,
    CURRENT_USER AS role_name,
    role.oid AS role_oid,
    role.rolsuper AS is_superuser,
    (
        role.rolsuper
        OR pg_catalog.pg_has_role(CURRENT_USER, 'pg_monitor', 'MEMBER')
        OR pg_catalog.pg_has_role(CURRENT_USER, 'pg_read_all_stats', 'MEMBER')
    ) AS has_monitoring_visibility,
    (
        role.rolsuper
        OR pg_catalog.pg_has_role(CURRENT_USER, 'pg_signal_backend', 'MEMBER')
    ) AS can_signal_backend
FROM pg_catalog.pg_database AS database
JOIN pg_catalog.pg_roles AS role
  ON role.rolname = CURRENT_USER
WHERE database.datname = pg_catalog.current_database()
"""

TABLE_TARGET_SQL = """
/* scm-ovs:table-target */
SELECT
    database.oid AS database_oid,
    relation.oid AS relation_oid,
    namespace.nspname AS schema_name,
    relation.relname AS relation_name,
    relation.relkind AS relation_kind,
    relation.relpersistence AS persistence,
    relation.relowner AS owner_oid,
    current_role.oid AS current_role_oid,
    database.datdba AS database_owner_oid,
    current_role.rolsuper AS current_is_superuser
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_database AS database
  ON database.datname = pg_catalog.current_database()
JOIN pg_catalog.pg_roles AS current_role
  ON current_role.rolname = CURRENT_USER
WHERE relation.oid = %s::oid
"""

INDEX_TARGET_SQL = """
/* scm-ovs:index-target */
SELECT
    database.oid AS database_oid,
    index_class.oid AS index_oid,
    index_catalog.indrelid AS parent_relation_oid,
    namespace.nspname AS schema_name,
    index_class.relname AS index_name,
    index_class.relkind AS index_kind,
    index_class.relpersistence AS index_persistence,
    parent_class.relkind AS parent_kind,
    parent_class.relowner AS owner_oid,
    current_role.oid AS current_role_oid,
    database.datdba AS database_owner_oid,
    current_role.rolsuper AS current_is_superuser,
    index_catalog.indisvalid AS is_valid,
    index_catalog.indisready AS is_ready,
    index_catalog.indislive AS is_live,
    index_catalog.indisexclusion AS is_exclusion
FROM pg_catalog.pg_class AS index_class
JOIN pg_catalog.pg_namespace AS namespace
  ON namespace.oid = index_class.relnamespace
JOIN pg_catalog.pg_index AS index_catalog
  ON index_catalog.indexrelid = index_class.oid
JOIN pg_catalog.pg_class AS parent_class
  ON parent_class.oid = index_catalog.indrelid
JOIN pg_catalog.pg_database AS database
  ON database.datname = pg_catalog.current_database()
JOIN pg_catalog.pg_roles AS current_role
  ON current_role.rolname = CURRENT_USER
WHERE namespace.nspname = %s
  AND index_class.relname = %s
  AND (%s::oid IS NULL OR index_class.oid = %s::oid)
"""

BACKEND_TARGET_SQL = """
/* scm-ovs:backend-target */
SELECT
    activity.datid AS database_oid,
    activity.pid,
    activity.backend_start,
    activity.xact_start AS transaction_start,
    activity.state_change,
    activity.state,
    activity.usename AS role_name,
    activity.application_name,
    activity.backend_type,
    target_role.rolsuper AS target_is_superuser,
    pg_catalog.pg_backend_pid() AS current_pid
FROM pg_catalog.pg_stat_activity AS activity
JOIN pg_catalog.pg_roles AS target_role
  ON target_role.rolname = activity.usename
WHERE activity.pid = %s
"""

MAINTAIN_PRIVILEGE_SQL = """
/* scm-ovs:maintain-privilege */
SELECT (
    pg_catalog.has_table_privilege(%s::oid, 'MAINTAIN')
    OR pg_catalog.pg_has_role(CURRENT_USER, 'pg_maintain', 'MEMBER')
) AS has_maintain
"""

ADVISORY_LOCK_SQL = """
/* scm-ovs:advisory-lock */
SELECT pg_catalog.pg_try_advisory_lock(%s::bigint) AS locked
"""

ADVISORY_UNLOCK_SQL = """
/* scm-ovs:advisory-unlock */
SELECT pg_catalog.pg_advisory_unlock(%s::bigint) AS unlocked
"""

TERMINATE_BACKEND_SQL = """
/* scm-ovs:terminate-backend */
SELECT pg_catalog.pg_terminate_backend(target.pid) AS terminated
FROM pg_catalog.pg_stat_activity AS target
JOIN pg_catalog.pg_roles AS target_role
  ON target_role.rolname = target.usename
WHERE target.datid = %s
  AND target.pid = %s
  AND target.backend_start = %s
  AND target.xact_start = %s
  AND target.state_change = %s
  AND target.state = %s
  AND target.usename = %s
  AND target.application_name = %s
  AND target.backend_type = 'client backend'
  AND target.pid <> pg_catalog.pg_backend_pid()
  AND target.application_name <> 'scm-ovs/healer'
  AND NOT target_role.rolsuper
  AND target.usename::text <> ALL(%s::text[])
  AND pg_catalog.clock_timestamp() - target.state_change
      >= pg_catalog.make_interval(secs => %s)
"""

TABLE_SNAPSHOT_SQL = """
/* scm-ovs:table-snapshot */
SELECT
    pg_catalog.clock_timestamp() AS observed_at,
    database.oid AS database_oid,
    stats.relid AS relation_oid,
    stats.schemaname AS schema_name,
    stats.relname AS relation_name,
    stats.n_live_tup,
    stats.n_dead_tup,
    stats.last_vacuum,
    stats.last_autovacuum,
    stats.vacuum_count,
    stats.autovacuum_count,
    pg_catalog.pg_relation_size(stats.relid) AS relation_size_bytes
FROM pg_catalog.pg_stat_user_tables AS stats
JOIN pg_catalog.pg_database AS database
  ON database.datname = pg_catalog.current_database()
WHERE stats.relid = %s::oid
"""

INDEX_SNAPSHOT_SQL = """
/* scm-ovs:index-snapshot */
SELECT
    pg_catalog.clock_timestamp() AS observed_at,
    database.oid AS database_oid,
    stats.relid AS parent_relation_oid,
    stats.indexrelid AS index_oid,
    stats.schemaname AS schema_name,
    stats.indexrelname AS index_name,
    stats.idx_scan,
    pg_catalog.pg_relation_size(stats.indexrelid) AS index_size_bytes,
    index_catalog.indisvalid AS is_valid,
    index_catalog.indisready AS is_ready,
    index_catalog.indislive AS is_live
FROM pg_catalog.pg_stat_user_indexes AS stats
JOIN pg_catalog.pg_index AS index_catalog
  ON index_catalog.indexrelid = stats.indexrelid
JOIN pg_catalog.pg_database AS database
  ON database.datname = pg_catalog.current_database()
WHERE stats.schemaname = %s
  AND stats.indexrelname = %s
  AND (%s::oid IS NULL OR stats.indexrelid = %s::oid)
"""

BACKEND_SNAPSHOT_SQL = """
/* scm-ovs:backend-snapshot */
SELECT
    pg_catalog.clock_timestamp() AS observed_at,
    activity.datid AS database_oid,
    activity.pid,
    activity.backend_start,
    activity.xact_start AS transaction_start,
    activity.state_change,
    activity.state,
    activity.usename AS role_name,
    activity.application_name,
    activity.backend_type,
    activity.wait_event_type,
    activity.wait_event
FROM pg_catalog.pg_stat_activity AS activity
WHERE activity.datid = %s
  AND activity.pid = %s
  AND activity.backend_start = %s
  AND activity.xact_start = %s
  AND activity.state_change = %s
  AND activity.state = %s
  AND activity.usename = %s
  AND activity.application_name = %s
  AND activity.backend_type = 'client backend'
"""


class _Cursor(Protocol):
    description: object

    def execute(self, sql: object, parameters: object = None) -> object: ...
    def fetchall(self) -> Sequence[object]: ...
    def fetchone(self) -> object: ...
    def __enter__(self) -> "_Cursor": ...
    def __exit__(self, *args: object) -> object: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...
    def __enter__(self) -> "_Connection": ...
    def __exit__(self, *args: object) -> object: ...


@dataclass(frozen=True)
class PostgresCapability:
    """Secret-free facts relevant to monitoring and action support."""

    driver_available: bool
    database_reachable: bool
    server_version_num: int | None = None
    server_version: str | None = None
    database_name: str | None = None
    database_oid: int | None = None
    role_name: str | None = None
    is_superuser: bool = False
    has_monitoring_visibility: bool = False
    can_signal_backend: bool = False
    supports_concurrent_reindex: bool = False
    maintain_privilege_supported: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityDecision:
    """A machine-readable precondition decision for one typed action plan."""

    allowed: bool
    reason: str
    capability: PostgresCapability | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _row_mapping(cursor: _Cursor, row: object) -> Mapping[str, object]:
    if isinstance(row, Mapping):
        return row
    description = cursor.description
    if not description:
        raise ValueError("query returned rows without column metadata")
    names: list[str] = []
    for column in description:  # type: ignore[union-attr]
        name = getattr(column, "name", None)
        if name is None and isinstance(column, Sequence):
            name = column[0]
        if not isinstance(name, str):
            raise ValueError("query returned invalid column metadata")
        names.append(name)
    if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
        raise ValueError("query returned an invalid row")
    if len(names) != len(row):
        raise ValueError("query row width did not match column metadata")
    return dict(zip(names, row))


def _required(row: Mapping[str, object], key: str, expected: type) -> object:
    value = row.get(key)
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise ValueError(f"required field {key} has an invalid value")
    return value


def _optional_datetime(row: Mapping[str, object], key: str) -> datetime | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"optional field {key} has an invalid value")
    return value


def _activity(row: Mapping[str, object]) -> ActivitySample:
    return ActivitySample(
        observed_at=_required(row, "observed_at", datetime),  # type: ignore[arg-type]
        database_oid=_required(row, "database_oid", int),  # type: ignore[arg-type]
        database_name=_required(row, "database_name", str),  # type: ignore[arg-type]
        pid=_required(row, "pid", int),  # type: ignore[arg-type]
        backend_start=_required(row, "backend_start", datetime),  # type: ignore[arg-type]
        role_name=_required(row, "role_name", str),  # type: ignore[arg-type]
        application_name=_required(row, "application_name", str),  # type: ignore[arg-type]
        backend_type=_required(row, "backend_type", str),  # type: ignore[arg-type]
        state=_required(row, "state", str),  # type: ignore[arg-type]
        state_change=_optional_datetime(row, "state_change"),
        transaction_start=_optional_datetime(row, "transaction_start"),
        query_start=_optional_datetime(row, "query_start"),
        wait_event_type=row.get("wait_event_type") if isinstance(row.get("wait_event_type"), str) else None,
        wait_event=row.get("wait_event") if isinstance(row.get("wait_event"), str) else None,
        is_self=_required(row, "is_self", bool),  # type: ignore[arg-type]
    )


def _table(row: Mapping[str, object]) -> TableStatsSample:
    return TableStatsSample(
        observed_at=_required(row, "observed_at", datetime),  # type: ignore[arg-type]
        database_oid=_required(row, "database_oid", int),  # type: ignore[arg-type]
        relation_oid=_required(row, "relation_oid", int),  # type: ignore[arg-type]
        schema_name=_required(row, "schema_name", str),  # type: ignore[arg-type]
        relation_name=_required(row, "relation_name", str),  # type: ignore[arg-type]
        n_live_tup=_required(row, "n_live_tup", int),  # type: ignore[arg-type]
        n_dead_tup=_required(row, "n_dead_tup", int),  # type: ignore[arg-type]
        last_autovacuum=_optional_datetime(row, "last_autovacuum"),
        last_vacuum=_optional_datetime(row, "last_vacuum"),
        vacuum_count=_required(row, "vacuum_count", int),  # type: ignore[arg-type]
        autovacuum_count=_required(row, "autovacuum_count", int),  # type: ignore[arg-type]
        relation_size_bytes=_required(row, "relation_size_bytes", int),  # type: ignore[arg-type]
    )


def _index(row: Mapping[str, object]) -> IndexStatsSample:
    return IndexStatsSample(
        observed_at=_required(row, "observed_at", datetime),  # type: ignore[arg-type]
        database_oid=_required(row, "database_oid", int),  # type: ignore[arg-type]
        table_oid=_required(row, "table_oid", int),  # type: ignore[arg-type]
        index_oid=_required(row, "index_oid", int),  # type: ignore[arg-type]
        schema_name=_required(row, "schema_name", str),  # type: ignore[arg-type]
        table_name=_required(row, "table_name", str),  # type: ignore[arg-type]
        index_name=_required(row, "index_name", str),  # type: ignore[arg-type]
        index_size_bytes=_required(row, "index_size_bytes", int),  # type: ignore[arg-type]
        idx_scan=_required(row, "idx_scan", int),  # type: ignore[arg-type]
        is_valid=_required(row, "is_valid", bool),  # type: ignore[arg-type]
        is_ready=_required(row, "is_ready", bool),  # type: ignore[arg-type]
        is_live=_required(row, "is_live", bool),  # type: ignore[arg-type]
        is_unique=_required(row, "is_unique", bool),  # type: ignore[arg-type]
        is_primary=_required(row, "is_primary", bool),  # type: ignore[arg-type]
        is_constraint=_required(row, "is_constraint", bool),  # type: ignore[arg-type]
        stats_reset=_optional_datetime(row, "stats_reset"),
        server_started=_optional_datetime(row, "server_started"),
    )


def _connection_sample(row: Mapping[str, object]) -> ConnectionSample:
    return ConnectionSample(
        observed_at=_required(row, "observed_at", datetime),  # type: ignore[arg-type]
        client_backend_count=_required(row, "client_backend_count", int),  # type: ignore[arg-type]
        maximum_connections=_required(row, "maximum_connections", int),  # type: ignore[arg-type]
        reserved_connections=_required(row, "reserved_connections", int),  # type: ignore[arg-type]
    )


class PostgresGateway:
    """Fixed-query collector and action gateway for one PostgreSQL target."""

    def __init__(
        self,
        config: HealingConfig,
        *,
        connect: Callable[..., _Connection] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._connect_function = connect
        self._clock = clock

    def _connect(self, application_name: str, *, autocommit: bool) -> _Connection:
        connector = self._connect_function
        if connector is None:
            connector = getattr(_load_psycopg(), "connect")
        return connector(
            self._config.database_dsn,
            application_name=application_name,
            autocommit=autocommit,
            connect_timeout=5,
        )

    def connect_monitor(self) -> _Connection:
        """Open the bounded, autocommit monitoring session."""
        return self._connect(_MONITOR_APPLICATION, autocommit=True)

    def _set_monitor_timeout(self, connection: _Connection) -> None:
        milliseconds = max(1, int(self._config.statement_timeout.total_seconds() * 1000))
        with connection.cursor() as cursor:
            cursor.execute(_SET_STATEMENT_TIMEOUT_SQL, (f"{milliseconds}ms",))

    def _collect_rows(
        self,
        connection: _Connection,
        sql: str,
        mapper: Callable[[Mapping[str, object]], object],
    ) -> tuple[object, ...]:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return tuple(mapper(_row_mapping(cursor, row)) for row in cursor.fetchall())

    def collect_health(self) -> HealthSnapshot:
        observed_at = self._clock()
        activities: tuple[ActivitySample, ...] = ()
        tables: tuple[TableStatsSample, ...] = ()
        indexes: tuple[IndexStatsSample, ...] = ()
        connection_sample: ConnectionSample | None = None
        errors: list[CollectionError] = []

        try:
            with self.connect_monitor() as connection:
                self._set_monitor_timeout(connection)
                collectors = (
                    ("activity", ACTIVITY_SQL, _activity),
                    ("tables", TABLE_STATS_SQL, _table),
                    ("indexes", INDEX_STATS_SQL, _index),
                    ("saturation", SATURATION_SQL, _connection_sample),
                )
                for name, sql, mapper in collectors:
                    try:
                        values = self._collect_rows(connection, sql, mapper)
                        if name == "activity":
                            activities = values  # type: ignore[assignment]
                        elif name == "tables":
                            tables = values  # type: ignore[assignment]
                        elif name == "indexes":
                            indexes = values  # type: ignore[assignment]
                        elif len(values) == 1:
                            connection_sample = values[0]  # type: ignore[assignment]
                        else:
                            raise ValueError("saturation collector returned an invalid row count")
                    except Exception as exc:
                        errors.append(CollectionError(name, _safe_collection_error(exc), observed_at))
        except PsycopgUnavailableError:
            raise
        except Exception as exc:
            message = _safe_collection_error(exc)
            for name in ("activity", "tables", "indexes", "saturation"):
                errors.append(CollectionError(name, message, observed_at))

        return HealthSnapshot(
            observed_at=observed_at,
            activities=activities,
            tables=tables,
            indexes=indexes,
            connection=connection_sample,
            errors=tuple(errors),
        )

    def _set_action_timeouts(self, connection: _Connection) -> None:
        lock_ms = max(1, int(self._config.lock_timeout.total_seconds() * 1000))
        statement_ms = max(1, int(self._config.statement_timeout.total_seconds() * 1000))
        with connection.cursor() as cursor:
            cursor.execute(_SET_LOCK_TIMEOUT_SQL, (f"{lock_ms}ms",))
            cursor.execute(_SET_STATEMENT_TIMEOUT_SQL, (f"{statement_ms}ms",))

    def _fetch_one(
        self,
        connection: _Connection,
        sql: str,
        parameters: object = None,
    ) -> Mapping[str, object] | None:
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            row = cursor.fetchone()
            return None if row is None else _row_mapping(cursor, row)

    def _capability(self, connection: _Connection) -> tuple[PostgresCapability, Mapping[str, object]]:
        row = self._fetch_one(connection, SERVER_FACTS_SQL)
        if row is None:
            raise ValueError("server facts query returned no row")
        version = _required(row, "server_version_num", int)
        capability = PostgresCapability(
            driver_available=True,
            database_reachable=True,
            server_version_num=version,  # type: ignore[arg-type]
            server_version=_required(row, "server_version", str),  # type: ignore[arg-type]
            database_name=_required(row, "database_name", str),  # type: ignore[arg-type]
            database_oid=_required(row, "database_oid", int),  # type: ignore[arg-type]
            role_name=_required(row, "role_name", str),  # type: ignore[arg-type]
            is_superuser=_required(row, "is_superuser", bool),  # type: ignore[arg-type]
            has_monitoring_visibility=_required(row, "has_monitoring_visibility", bool),  # type: ignore[arg-type]
            can_signal_backend=_required(row, "can_signal_backend", bool),  # type: ignore[arg-type]
            supports_concurrent_reindex=version >= 120000,  # type: ignore[operator]
            maintain_privilege_supported=version >= 170000,  # type: ignore[operator]
        )
        return capability, row

    def _check_plan_on_connection(
        self,
        plan: ActionPlan,
        connection: _Connection,
        *,
        expected_identity: Mapping[str, object] | None = None,
    ) -> CapabilityDecision:
        capability, _ = self._capability(connection)
        if capability.server_version_num is None or capability.server_version_num < 120000:
            return CapabilityDecision(False, "unsupported_server_version", capability)
        if plan.mode is Mode.OBSERVE:
            return CapabilityDecision(False, "observe_mode", capability)

        canonical_reason = _canonical_plan_reason(plan)
        if canonical_reason is not None:
            return CapabilityDecision(False, canonical_reason, capability)

        if isinstance(plan.target, TableTarget):
            row = self._fetch_one(connection, TABLE_TARGET_SQL, (plan.target.relation_oid,))
            decision = self._check_table(plan.target, row, capability, connection)
        elif isinstance(plan.target, IndexTarget):
            row = self._fetch_one(
                connection,
                INDEX_TARGET_SQL,
                (
                    plan.target.schema_name,
                    plan.target.index_name,
                    plan.target.index_oid,
                    plan.target.index_oid,
                ),
            )
            decision = self._check_index(plan.target, row, capability, connection)
        else:
            row = self._fetch_one(connection, BACKEND_TARGET_SQL, (plan.target.pid,))
            decision = self._check_backend(plan.target, row, capability)

        if decision.allowed and expected_identity is not None:
            current = decision.evidence.get("identity")
            if current != expected_identity:
                return CapabilityDecision(
                    False,
                    "stale_backend_identity" if isinstance(plan.target, BackendTarget)
                    else "stale_relation_identity",
                    capability,
                )
        return decision

    def _check_table(
        self,
        target: TableTarget,
        row: Mapping[str, object] | None,
        capability: PostgresCapability,
        connection: _Connection,
    ) -> CapabilityDecision:
        if row is not None and isinstance(row.get("schema_name"), str) and _is_system_schema(row["schema_name"]):
            return CapabilityDecision(False, "system_relation_rejected", capability)
        if row is None or any(
            row.get(key) != value
            for key, value in (
                ("database_oid", target.database_oid),
                ("relation_oid", target.relation_oid),
                ("schema_name", target.schema_name),
                ("relation_name", target.relation_name),
            )
        ):
            return CapabilityDecision(False, "stale_relation_identity", capability)
        if row.get("relation_kind") != "r":
            return CapabilityDecision(False, "unsupported_relation_type", capability)
        if row.get("persistence") != "p":
            return CapabilityDecision(False, "temporary_relation_rejected", capability)
        if not _has_relation_authority(row):
            has_maintain = False
            if capability.maintain_privilege_supported:
                privilege = self._fetch_one(
                    connection, MAINTAIN_PRIVILEGE_SQL, (target.relation_oid,)
                )
                has_maintain = privilege is not None and privilege.get("has_maintain") is True
            if not has_maintain:
                return CapabilityDecision(False, "insufficient_privilege", capability)
        return CapabilityDecision(True, "ok", capability, {"identity": _table_identity(row)})

    def _check_index(
        self,
        target: IndexTarget,
        row: Mapping[str, object] | None,
        capability: PostgresCapability,
        connection: _Connection,
    ) -> CapabilityDecision:
        if row is not None and isinstance(row.get("schema_name"), str) and _is_system_schema(row["schema_name"]):
            return CapabilityDecision(False, "system_relation_rejected", capability)
        if row is None or any(
            row.get(key) != value
            for key, value in (
                ("schema_name", target.schema_name),
                ("index_name", target.index_name),
            )
        ):
            return CapabilityDecision(False, "stale_relation_identity", capability)
        if target.database_oid is not None and row.get("database_oid") != target.database_oid:
            return CapabilityDecision(False, "stale_relation_identity", capability)
        if target.index_oid is not None and row.get("index_oid") != target.index_oid:
            return CapabilityDecision(False, "stale_relation_identity", capability)
        if target.parent_relation_oid is not None and row.get("parent_relation_oid") != target.parent_relation_oid:
            return CapabilityDecision(False, "stale_relation_identity", capability)
        if row.get("index_kind") != "i" or row.get("parent_kind") not in ("r", "p"):
            return CapabilityDecision(False, "unsupported_relation_type", capability)
        if row.get("index_persistence") != "p":
            return CapabilityDecision(False, "temporary_relation_rejected", capability)
        if not all(row.get(key) is True for key in ("is_valid", "is_ready", "is_live")):
            return CapabilityDecision(False, "ineligible_index_state", capability)
        if row.get("is_exclusion") is True:
            return CapabilityDecision(False, "exclusion_index_rejected", capability)
        if not _has_index_authority(row):
            has_maintain = False
            if capability.maintain_privilege_supported:
                privilege = self._fetch_one(
                    connection, MAINTAIN_PRIVILEGE_SQL, (row.get("parent_relation_oid"),)
                )
                has_maintain = privilege is not None and privilege.get("has_maintain") is True
            if not has_maintain:
                return CapabilityDecision(False, "insufficient_privilege", capability)
        return CapabilityDecision(True, "ok", capability, {"identity": _index_identity(row)})

    def _check_backend(
        self,
        target: BackendTarget,
        row: Mapping[str, object] | None,
        capability: PostgresCapability,
    ) -> CapabilityDecision:
        if not capability.is_superuser and not capability.can_signal_backend:
            return CapabilityDecision(False, "insufficient_privilege", capability)
        if row is None:
            return CapabilityDecision(False, "stale_backend_identity", capability)
        if row.get("pid") == row.get("current_pid"):
            return CapabilityDecision(False, "self_backend_rejected", capability)
        if row.get("application_name") == _HEALER_APPLICATION:
            return CapabilityDecision(False, "self_backend_rejected", capability)
        if row.get("target_is_superuser") is True:
            return CapabilityDecision(False, "superuser_target_rejected", capability)
        if row.get("role_name") in self._config.protected_roles:
            return CapabilityDecision(False, "protected_role", capability)
        expected = {
            "database_oid": target.database_oid,
            "pid": target.pid,
            "backend_start": target.backend_start,
            "transaction_start": target.transaction_start,
            "state_change": target.state_change,
            "state": target.state,
            "role_name": target.role_name,
            "application_name": target.application_name,
            "backend_type": "client backend",
        }
        if any(row.get(key) != value for key, value in expected.items()):
            return CapabilityDecision(False, "stale_backend_identity", capability)
        if target.state != "idle in transaction":
            return CapabilityDecision(False, "unsafe_backend_state", capability)
        return CapabilityDecision(True, "ok", capability, {"identity": expected})

    def _runtime_policy_decision(self, plan: ActionPlan) -> CapabilityDecision:
        """Reconcile a persisted plan with the gateway's current safe policy."""
        if self._config.mode is Mode.OBSERVE:
            return CapabilityDecision(False, "gateway_observe_mode")
        if plan.mode is not self._config.mode:
            return CapabilityDecision(
                False,
                "gateway_mode_mismatch",
                evidence={
                    "gateway_mode": self._config.mode.value,
                    "plan_mode": plan.mode.value,
                },
            )
        if (
            plan.action_kind is ActionKind.TERMINATE_BACKEND
            and not self._config.allow_terminate_idle_transaction
        ):
            return CapabilityDecision(False, "terminate_backend_disabled")
        if (
            plan.action_kind is ActionKind.REINDEX_INDEX
            and not self._config.allow_manual_reindex
        ):
            return CapabilityDecision(False, "manual_reindex_disabled")
        return CapabilityDecision(True, "ok")

    def check_plan(self, plan: ActionPlan) -> CapabilityDecision:
        """Check capabilities and the complete live target identity without acting."""
        policy = self._runtime_policy_decision(plan)
        if not policy.allowed:
            return policy
        try:
            with self._connect(_HEALER_APPLICATION, autocommit=True) as connection:
                self._set_action_timeouts(connection)
                return self._check_plan_on_connection(plan, connection)
        except PsycopgUnavailableError:
            raise
        except Exception as exc:
            return CapabilityDecision(
                False,
                _reason_for_sqlstate(_sqlstate(exc)),
                evidence={"sqlstate": _sqlstate(exc)} if _sqlstate(exc) else {},
            )

    def authorize_plan(self, plan: ActionPlan) -> PlanAuthorization:
        """Resolve a manual index name to the exact OID-bound plan to persist."""

        policy = self._runtime_policy_decision(plan)
        if not policy.allowed:
            return PlanAuthorization(policy, plan)
        try:
            with self._connect(_HEALER_APPLICATION, autocommit=True) as connection:
                self._set_action_timeouts(connection)
                decision = self._check_plan_on_connection(plan, connection)
                if not decision.allowed or not isinstance(plan.target, IndexTarget):
                    return PlanAuthorization(decision, plan)
                identity = decision.evidence.get("identity")
                if not isinstance(identity, Mapping):
                    return PlanAuthorization(
                        CapabilityDecision(False, "stale_relation_identity"), plan
                    )
                try:
                    target = IndexTarget(
                        schema_name=plan.target.schema_name,
                        index_name=plan.target.index_name,
                        database_oid=_required(identity, "database_oid", int),
                        index_oid=_required(identity, "index_oid", int),
                        parent_relation_oid=_required(
                            identity, "parent_relation_oid", int
                        ),
                    )
                except (TypeError, ValueError):
                    return PlanAuthorization(
                        CapabilityDecision(False, "stale_relation_identity"), plan
                    )
                authorized = replace(
                    plan, target=target, parameters=target.parameters
                )
                return PlanAuthorization(decision, authorized)
        except PsycopgUnavailableError:
            raise
        except Exception as exc:
            sqlstate = _sqlstate(exc)
            return PlanAuthorization(
                CapabilityDecision(
                    False,
                    _reason_for_sqlstate(sqlstate),
                    evidence={"sqlstate": sqlstate} if sqlstate else {},
                ),
                plan,
            )

    def doctor(self) -> Mapping[str, object]:
        """Return live capability facts without echoing DSNs or database errors."""
        if self._connect_function is None and not _driver_is_available():
            return asdict(
                PostgresCapability(
                    driver_available=False,
                    database_reachable=False,
                    notes=(f"Install the PostgreSQL driver with {_INSTALL_GUIDANCE}",),
                )
            )
        try:
            with self.connect_monitor() as connection:
                self._set_monitor_timeout(connection)
                capability, _ = self._capability(connection)
                return asdict(capability)
        except Exception as exc:
            sqlstate = _sqlstate(exc)
            return asdict(
                PostgresCapability(
                    driver_available=True,
                    database_reachable=False,
                    notes=(_safe_action_error(_reason_for_sqlstate(sqlstate), sqlstate),),
                )
            )

    def _capture(self, plan: ActionPlan) -> Mapping[str, object]:
        with self.connect_monitor() as connection:
            self._set_monitor_timeout(connection)
            if isinstance(plan.target, TableTarget):
                row = self._fetch_one(
                    connection, TABLE_SNAPSHOT_SQL, (plan.target.relation_oid,)
                )
                kind = "table"
            elif isinstance(plan.target, IndexTarget):
                row = self._fetch_one(
                    connection,
                    INDEX_SNAPSHOT_SQL,
                    (
                        plan.target.schema_name,
                        plan.target.index_name,
                        plan.target.index_oid,
                        plan.target.index_oid,
                    ),
                )
                kind = "index"
            else:
                target = plan.target
                row = self._fetch_one(
                    connection,
                    BACKEND_SNAPSHOT_SQL,
                    (
                        target.database_oid,
                        target.pid,
                        target.backend_start,
                        target.transaction_start,
                        target.state_change,
                        target.state,
                        target.role_name,
                        target.application_name,
                    ),
                )
                kind = "backend"
            if row is None:
                return {"present": False, "target_kind": kind}
            return {"present": True, "target_kind": kind, **dict(row)}

    def capture_before(self, plan: ActionPlan) -> Mapping[str, object]:
        """Capture relevant catalog/statistics state before an action attempt."""
        return self._capture(plan)

    def capture_after(self, plan: ActionPlan) -> Mapping[str, object]:
        """Capture relevant catalog/statistics state after an action attempt."""
        return self._capture(plan)

    def execute(self, plan: ActionPlan) -> ActionResult:
        """Revalidate, serialize, and execute one fixed action on autocommit."""
        started = self._clock()
        policy = self._runtime_policy_decision(plan)
        if not policy.allowed:
            return _decision_result(plan, policy, started, self._clock())
        if (
            plan.mode is Mode.ACT
            and isinstance(plan.target, IndexTarget)
            and (
                plan.target.database_oid is None
                or plan.target.index_oid is None
                or plan.target.parent_relation_oid is None
            )
        ):
            return _decision_result(
                plan,
                CapabilityDecision(False, "unresolved_index_identity"),
                started,
                self._clock(),
            )
        command_attempted = False
        lock_key: int | None = None
        try:
            with self._connect(_HEALER_APPLICATION, autocommit=True) as connection:
                self._set_action_timeouts(connection)
                first = self._check_plan_on_connection(plan, connection)
                if not first.allowed:
                    return _decision_result(plan, first, started, self._clock())
                if plan.mode is Mode.DRY_RUN:
                    return ActionResult(
                        plan.action_kind,
                        ActionStatus.SIMULATED,
                        plan.target_key,
                        started,
                        self._clock(),
                        evidence={"reason": "dry_run"},
                    )

                lock_key = _advisory_key(plan)
                locked = self._fetch_one(connection, ADVISORY_LOCK_SQL, (lock_key,))
                if locked is None or locked.get("locked") is not True:
                    decision = CapabilityDecision(False, "advisory_lock_unavailable")
                    return _decision_result(plan, decision, started, self._clock())
                try:
                    identity = first.evidence.get("identity")
                    assert isinstance(identity, Mapping)
                    immediate = self._check_plan_on_connection(
                        plan, connection, expected_identity=identity
                    )
                    if not immediate.allowed:
                        return _decision_result(plan, immediate, started, self._clock())

                    command_attempted = True
                    return self._execute_fixed(plan, connection, started)
                finally:
                    try:
                        self._fetch_one(connection, ADVISORY_UNLOCK_SQL, (lock_key,))
                    except Exception:
                        pass
        except PsycopgUnavailableError:
            raise
        except Exception as exc:
            sqlstate = _sqlstate(exc)
            reason = _reason_for_sqlstate(sqlstate)
            status = (
                ActionStatus.OUTCOME_UNKNOWN
                if command_attempted and sqlstate is not None and sqlstate.startswith("08")
                else ActionStatus.FAILED
            )
            return ActionResult(
                plan.action_kind,
                status,
                plan.target_key,
                started,
                self._clock(),
                error=_safe_action_error(reason, sqlstate),
                evidence={"reason": reason, **({"sqlstate": sqlstate} if sqlstate else {})},
            )

    def _execute_fixed(
        self, plan: ActionPlan, connection: _Connection, started: datetime
    ) -> ActionResult:
        if isinstance(plan.target, BackendTarget):
            target = plan.target
            with connection.cursor() as cursor:
                cursor.execute(
                    TERMINATE_BACKEND_SQL,
                    (
                        target.database_oid,
                        target.pid,
                        target.backend_start,
                        target.transaction_start,
                        target.state_change,
                        target.state,
                        target.role_name,
                        target.application_name,
                        list(self._config.protected_roles),
                        self._config.policy.idle_transaction_action.total_seconds(),
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    decision = CapabilityDecision(False, "stale_backend_identity")
                    return _decision_result(plan, decision, started, self._clock())
                mapped = _row_mapping(cursor, row)
                if mapped.get("terminated") is not True:
                    return ActionResult(
                        plan.action_kind,
                        ActionStatus.FAILED,
                        plan.target_key,
                        started,
                        self._clock(),
                        error="termination_returned_false",
                        evidence={"reason": "termination_returned_false"},
                    )
        else:
            command = (
                build_vacuum_command(plan.target.schema_name, plan.target.relation_name)
                if isinstance(plan.target, TableTarget)
                else build_reindex_command(plan.target.schema_name, plan.target.index_name)
            )
            with connection.cursor() as cursor:
                cursor.execute(command)
        return ActionResult(
            plan.action_kind,
            ActionStatus.SUCCEEDED,
            plan.target_key,
            started,
            self._clock(),
            evidence={"reason": "action_succeeded"},
        )


def _canonical_plan_reason(plan: ActionPlan) -> str | None:
    if isinstance(plan.target, TableTarget):
        expected_kind = ActionKind.VACUUM
        expected_command = build_vacuum_command(
            plan.target.schema_name, plan.target.relation_name
        )
    elif isinstance(plan.target, IndexTarget):
        expected_kind = ActionKind.REINDEX_INDEX
        expected_command = build_reindex_command(plan.target.schema_name, plan.target.index_name)
    else:
        expected_kind = ActionKind.TERMINATE_BACKEND
        expected_command = "SELECT pg_terminate_backend(?)"
    if plan.action_kind is not expected_kind or plan.command != expected_command:
        return "invalid_command"
    return None


def _is_system_schema(schema: str) -> bool:
    normalized = schema.casefold()
    return normalized in ("pg_catalog", "information_schema", "pg_toast") or normalized.startswith("pg_")


def _has_relation_authority(row: Mapping[str, object]) -> bool:
    return (
        row.get("current_is_superuser") is True
        or row.get("owner_oid") == row.get("current_role_oid")
        or row.get("database_owner_oid") == row.get("current_role_oid")
    )


def _has_index_authority(row: Mapping[str, object]) -> bool:
    return (
        row.get("current_is_superuser") is True
        or row.get("owner_oid") == row.get("current_role_oid")
    )


def _table_identity(row: Mapping[str, object]) -> Mapping[str, object]:
    keys = (
        "database_oid",
        "relation_oid",
        "schema_name",
        "relation_name",
        "relation_kind",
        "persistence",
    )
    return {key: row.get(key) for key in keys}


def _index_identity(row: Mapping[str, object]) -> Mapping[str, object]:
    keys = (
        "database_oid",
        "index_oid",
        "parent_relation_oid",
        "schema_name",
        "index_name",
        "index_kind",
        "index_persistence",
        "parent_kind",
        "is_valid",
        "is_ready",
        "is_live",
        "is_exclusion",
    )
    return {key: row.get(key) for key in keys}


def _advisory_key(plan: ActionPlan) -> int:
    digest = sha256(
        f"scm-ovs\x00{plan.action_kind.value}\x00{plan.target_key}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _decision_result(
    plan: ActionPlan,
    decision: CapabilityDecision,
    started: datetime,
    completed: datetime,
) -> ActionResult:
    evidence = {"reason": decision.reason}
    if decision.evidence:
        evidence["precondition"] = dict(decision.evidence)
    return ActionResult(
        plan.action_kind,
        ActionStatus.PRECONDITION_REJECTED,
        plan.target_key,
        started,
        completed,
        error=decision.reason,
        evidence=evidence,
    )


def _reason_for_sqlstate(sqlstate: str | None) -> str:
    if sqlstate == "42501":
        return "insufficient_privilege"
    if sqlstate == "55P03":
        return "lock_timeout"
    if sqlstate == "57014":
        return "statement_timeout"
    if sqlstate is not None and sqlstate.startswith("08"):
        return "connection_lost"
    return "database_error"


def _safe_action_error(reason: str, sqlstate: str | None) -> str:
    return f"{reason}{f' (SQLSTATE {sqlstate})' if sqlstate else ''}"


def _safe_collection_error(error: BaseException) -> str:
    if isinstance(error, (TypeError, ValueError)):
        return str(error)[:240]
    sqlstate = _sqlstate(error)
    return f"collector query failed{f' (SQLSTATE {sqlstate})' if sqlstate else ''}"


def _sqlstate(error: BaseException) -> str | None:
    direct = getattr(error, "sqlstate", None)
    if isinstance(direct, str) and len(direct) == 5:
        return direct
    diagnostic = getattr(error, "diag", None)
    value = getattr(diagnostic, "sqlstate", None)
    return value if isinstance(value, str) and len(value) == 5 else None


__all__ = [
    "ACTIVITY_SQL",
    "INDEX_STATS_SQL",
    "SATURATION_SQL",
    "TABLE_STATS_SQL",
    "CapabilityDecision",
    "PostgresCapability",
    "PostgresGateway",
    "PsycopgUnavailableError",
]
