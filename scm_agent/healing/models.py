"""Immutable domain values shared by the healing engine layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping


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


@dataclass(frozen=True)
class ActionPlan:
    action_kind: ActionKind
    finding_fingerprint: str
    target_kind: TargetKind
    target_key: str
    command: str
    parameters: Mapping[str, object]
    mode: Mode
    reason: str


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
