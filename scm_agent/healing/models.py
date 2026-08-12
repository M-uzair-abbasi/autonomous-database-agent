"""Immutable domain values shared by the healing engine layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterator, Mapping


class Mode(str, Enum):
    OBSERVE = "observe"
    DRY_RUN = "dry_run"
    ACT = "act"


class Severity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


class RuleId(str, Enum):
    DEAD_TUPLE_PRESSURE = "dead_tuple_pressure"
    IDLE_IN_TRANSACTION = "idle_in_transaction"
    LONG_RUNNING_QUERY = "long_running_query"
    CONNECTION_SATURATION = "connection_saturation"
    UNUSED_LARGE_INDEX = "unused_large_index"


class TargetKind(str, Enum):
    TABLE = "table"
    BACKEND = "backend"
    CLUSTER = "cluster"
    INDEX = "index"


class ActionKind(str, Enum):
    VACUUM = "vacuum"
    TERMINATE_BACKEND = "terminate_backend"
    REINDEX_INDEX = "reindex_index"


class ActionStatus(str, Enum):
    PLANNED = "planned"
    PRECONDITION_REJECTED = "precondition_rejected"
    SIMULATED = "simulated"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"


def _require_aware(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


class FrozenMapping(Mapping[str, object]):
    """A copy-on-construction mapping that remains audit-serialization safe."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, object]) -> None:
        self._items = tuple((key, _freeze_value(value)) for key, value in values.items())

    def __getitem__(self, key: str) -> object:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, memo: dict[int, object]) -> "FrozenMapping":
        return self


def _positive_oid(value: int | None, field_name: str, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        suffix = "must be a positive integer" if required else "must be a positive integer when present"
        raise ValueError(f"{field_name} {suffix}")


def _nonempty_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")


@dataclass(frozen=True)
class TableTarget:
    """The stable catalog identity required to vacuum one user table."""

    database_oid: int
    relation_oid: int
    schema_name: str
    relation_name: str

    def __post_init__(self) -> None:
        _positive_oid(self.database_oid, "database_oid", required=True)
        _positive_oid(self.relation_oid, "relation_oid", required=True)
        _nonempty_text(self.schema_name, "schema_name")
        _nonempty_text(self.relation_name, "relation_name")

    @property
    def parameters(self) -> FrozenMapping:
        return FrozenMapping({
            "database_oid": self.database_oid,
            "relation_oid": self.relation_oid,
            "schema": self.schema_name,
            "table": self.relation_name,
        })


@dataclass(frozen=True)
class BackendTarget:
    """Captured exact session identity, never merely a recyclable backend PID."""

    database_oid: int
    pid: int
    backend_start: datetime
    transaction_start: datetime
    state_change: datetime
    state: str
    role_name: str
    application_name: str

    def __post_init__(self) -> None:
        _positive_oid(self.database_oid, "database_oid", required=True)
        _positive_oid(self.pid, "pid", required=True)
        for name in ("backend_start", "transaction_start", "state_change"):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                raise ValueError(f"{name} must be a timezone-aware datetime")
            _require_aware(value, name)
        _nonempty_text(self.state, "state")
        _nonempty_text(self.role_name, "role_name")
        _nonempty_text(self.application_name, "application_name")

    @property
    def parameters(self) -> FrozenMapping:
        return FrozenMapping({
            "database_oid": self.database_oid,
            "pid": self.pid,
            "backend_start": self.backend_start,
            "transaction_start": self.transaction_start,
            "state_change": self.state_change,
            "state": self.state,
            "role_name": self.role_name,
            "application_name": self.application_name,
        })


@dataclass(frozen=True)
class IndexTarget:
    """Manual index name identity, optionally enriched by Task 4 OID lookup."""

    schema_name: str
    index_name: str
    database_oid: int | None = None
    index_oid: int | None = None
    parent_relation_oid: int | None = None

    def __post_init__(self) -> None:
        _nonempty_text(self.schema_name, "schema_name")
        _nonempty_text(self.index_name, "index_name")
        _positive_oid(self.database_oid, "database_oid", required=False)
        _positive_oid(self.index_oid, "index_oid", required=False)
        _positive_oid(self.parent_relation_oid, "parent_relation_oid", required=False)

    @property
    def parameters(self) -> FrozenMapping:
        return FrozenMapping({
            "schema": self.schema_name,
            "index": self.index_name,
            "database_oid": self.database_oid,
            "index_oid": self.index_oid,
            "parent_relation_oid": self.parent_relation_oid,
        })


ActionTarget = TableTarget | BackendTarget | IndexTarget


def _target_kind_for(target: ActionTarget) -> TargetKind:
    if isinstance(target, TableTarget):
        return TargetKind.TABLE
    if isinstance(target, BackendTarget):
        return TargetKind.BACKEND
    return TargetKind.INDEX


@dataclass(frozen=True)
class ActivitySample:
    observed_at: datetime
    database_oid: int
    database_name: str
    pid: int
    backend_start: datetime
    role_name: str
    application_name: str
    backend_type: str
    state: str
    state_change: datetime | None
    transaction_start: datetime | None = None
    query_start: datetime | None = None
    wait_event_type: str | None = None
    wait_event: str | None = None
    is_self: bool = False

    def __post_init__(self) -> None:
        for name in (
            "observed_at",
            "backend_start",
            "state_change",
            "transaction_start",
            "query_start",
        ):
            _require_aware(getattr(self, name), name)


@dataclass(frozen=True)
class TableStatsSample:
    observed_at: datetime
    database_oid: int
    relation_oid: int
    schema_name: str
    relation_name: str
    n_live_tup: int
    n_dead_tup: int
    last_autovacuum: datetime | None = None
    last_vacuum: datetime | None = None
    vacuum_count: int = 0
    autovacuum_count: int = 0
    relation_size_bytes: int = 0

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.last_autovacuum, "last_autovacuum")
        _require_aware(self.last_vacuum, "last_vacuum")


@dataclass(frozen=True)
class IndexStatsSample:
    observed_at: datetime
    database_oid: int
    table_oid: int
    index_oid: int
    schema_name: str
    table_name: str
    index_name: str
    index_size_bytes: int
    idx_scan: int
    is_valid: bool = True
    is_ready: bool = True
    is_live: bool = True
    is_unique: bool = False
    is_primary: bool = False
    is_constraint: bool = False
    stats_reset: datetime | None = None
    server_started: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("observed_at", "stats_reset", "server_started"):
            _require_aware(getattr(self, name), name)


@dataclass(frozen=True)
class ConnectionSample:
    observed_at: datetime
    client_backend_count: int
    maximum_connections: int
    reserved_connections: int = 0

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class CollectionError:
    collector: str
    message: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class HealthSnapshot:
    observed_at: datetime
    activities: tuple[ActivitySample, ...] = ()
    tables: tuple[TableStatsSample, ...] = ()
    indexes: tuple[IndexStatsSample, ...] = ()
    connection: ConnectionSample | None = None
    errors: tuple[CollectionError, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class Finding:
    rule_id: RuleId
    severity: Severity
    target_kind: TargetKind
    target_key: str
    evidence: Mapping[str, object]
    recommendation: str
    action_eligible: bool
    observed_at: datetime
    fingerprint: str
    rule_version: str = "v1"

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        object.__setattr__(self, "evidence", FrozenMapping(self.evidence))


@dataclass(frozen=True)
class ActionPlan:
    action_kind: ActionKind
    finding_fingerprint: str
    target_kind: TargetKind
    target: ActionTarget
    target_key: str
    command: str
    parameters: Mapping[str, object]
    mode: Mode
    reason: str

    def __post_init__(self) -> None:
        if self.target_kind is not _target_kind_for(self.target):
            raise ValueError("target_kind does not match typed target")
        expected_action = {
            TableTarget: ActionKind.VACUUM,
            BackendTarget: ActionKind.TERMINATE_BACKEND,
            IndexTarget: ActionKind.REINDEX_INDEX,
        }[type(self.target)]
        if self.action_kind is not expected_action:
            raise ValueError("action_kind does not match typed target")
        derived_parameters = self.target.parameters
        if self.parameters and dict(self.parameters) != dict(derived_parameters):
            raise ValueError("parameters must be derived from the typed target")
        object.__setattr__(self, "parameters", derived_parameters)


@dataclass(frozen=True)
class ActionResult:
    action_kind: ActionKind
    status: ActionStatus
    target_key: str
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        _require_aware(self.completed_at, "completed_at")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        object.__setattr__(self, "evidence", FrozenMapping(self.evidence))
