"""Conservative configuration for the self-healing engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Mapping

from .models import Mode


_TRUE_VALUES = frozenset(("1", "true", "yes", "on"))
_FALSE_VALUES = frozenset(("0", "false", "no", "off"))
_DEFAULT_DSN = "postgresql://scm:scm@localhost:5434/scm_demo"


def _positive(value: int | float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean (1/true/yes/on or 0/false/no/off)")


def _parse_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class RulePolicy:
    dead_tuple_ratio: float = 0.20
    minimum_table_rows: int = 10_000
    minimum_dead_tuples: int = 5_000
    idle_transaction_warning: timedelta = timedelta(minutes=5)
    idle_transaction_action: timedelta = timedelta(minutes=15)
    long_query: timedelta = timedelta(minutes=5)
    connection_warning_ratio: float = 0.80
    connection_critical_ratio: float = 0.90
    unused_index_minimum_bytes: int = 100 * 1024 * 1024
    unused_index_maximum_scans: int = 0
    minimum_statistics_age: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if not 0 < self.dead_tuple_ratio < 1:
            raise ValueError("dead_tuple_ratio must be between zero and one")
        _positive(self.minimum_table_rows, "minimum_table_rows")
        _positive(self.minimum_dead_tuples, "minimum_dead_tuples")
        _positive(self.idle_transaction_warning.total_seconds(), "idle_transaction_warning")
        _positive(self.idle_transaction_action.total_seconds(), "idle_transaction_action")
        if self.idle_transaction_action < self.idle_transaction_warning:
            raise ValueError("idle_transaction_action must not be shorter than warning")
        _positive(self.long_query.total_seconds(), "long_query")
        if not 0 < self.connection_warning_ratio < self.connection_critical_ratio <= 1:
            raise ValueError("connection warning and critical ratios must be ordered within (0, 1]")
        _positive(self.unused_index_minimum_bytes, "unused_index_minimum_bytes")
        if self.unused_index_maximum_scans < 0:
            raise ValueError("unused_index_maximum_scans must not be negative")
        _positive(self.minimum_statistics_age.total_seconds(), "minimum_statistics_age")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "RulePolicy":
        return cls(
            dead_tuple_ratio=_parse_float(environ, "SCM_HEALING_DEAD_TUPLE_RATIO", 0.20),
            minimum_table_rows=_parse_int(environ, "SCM_HEALING_MINIMUM_TABLE_ROWS", 10_000),
            minimum_dead_tuples=_parse_int(environ, "SCM_HEALING_MINIMUM_DEAD_TUPLES", 5_000),
            idle_transaction_warning=timedelta(seconds=_parse_int(environ, "SCM_HEALING_IDLE_TRANSACTION_WARNING_SECONDS", 300)),
            idle_transaction_action=timedelta(seconds=_parse_int(environ, "SCM_HEALING_IDLE_TRANSACTION_ACTION_SECONDS", 900)),
            long_query=timedelta(seconds=_parse_int(environ, "SCM_HEALING_LONG_QUERY_SECONDS", 300)),
            connection_warning_ratio=_parse_float(environ, "SCM_HEALING_CONNECTION_WARNING_RATIO", 0.80),
            connection_critical_ratio=_parse_float(environ, "SCM_HEALING_CONNECTION_CRITICAL_RATIO", 0.90),
            unused_index_minimum_bytes=_parse_int(environ, "SCM_HEALING_UNUSED_INDEX_MINIMUM_BYTES", 100 * 1024 * 1024),
            unused_index_maximum_scans=_parse_int(environ, "SCM_HEALING_UNUSED_INDEX_MAXIMUM_SCANS", 0),
            minimum_statistics_age=timedelta(seconds=_parse_int(environ, "SCM_HEALING_MINIMUM_STATISTICS_AGE_SECONDS", 7 * 24 * 60 * 60)),
        )


@dataclass(frozen=True)
class HealingConfig:
    database_dsn: str = _DEFAULT_DSN
    mode: Mode = Mode.OBSERVE
    policy: RulePolicy = field(default_factory=RulePolicy)
    audit_path: Path = Path("var/scm_ovs/healing-audit.sqlite3")
    poll_interval: timedelta = timedelta(seconds=30)
    maximum_actions_per_cycle: int = 1
    required_consecutive_detections: int = 2
    vacuum_cooldown: timedelta = timedelta(hours=6)
    lock_timeout: timedelta = timedelta(seconds=5)
    statement_timeout: timedelta = timedelta(seconds=60)
    protected_roles: tuple[str, ...] = ("postgres",)
    allow_terminate_idle_transaction: bool = False
    allow_manual_reindex: bool = False

    def __post_init__(self) -> None:
        if not self.database_dsn.strip():
            raise ValueError("database_dsn must not be empty")
        if not str(self.audit_path).strip():
            raise ValueError("audit_path must not be empty")
        _positive(self.poll_interval.total_seconds(), "poll_interval")
        _positive(self.maximum_actions_per_cycle, "maximum_actions_per_cycle")
        _positive(self.required_consecutive_detections, "required_consecutive_detections")
        if self.vacuum_cooldown < timedelta(hours=6):
            raise ValueError("vacuum_cooldown must be at least six hours")
        _positive(self.lock_timeout.total_seconds(), "lock_timeout")
        _positive(self.statement_timeout.total_seconds(), "statement_timeout")
        if self.statement_timeout < self.lock_timeout:
            raise ValueError("statement_timeout must not be shorter than lock_timeout")
        if any(not role.strip() for role in self.protected_roles):
            raise ValueError("protected_roles must not contain empty roles")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HealingConfig":
        values: Mapping[str, str] = {} if environ is None else environ
        raw_mode = values.get("SCM_HEALING_MODE", Mode.OBSERVE.value).strip().lower()
        try:
            mode = Mode(raw_mode)
        except ValueError as exc:
            raise ValueError("mode must be observe, dry_run, or act") from exc

        audit_path = values.get("SCM_HEALING_AUDIT_PATH", "var/scm_ovs/healing-audit.sqlite3")
        if not audit_path.strip():
            raise ValueError("audit_path must not be empty")
        protected_roles = tuple(
            role.strip() for role in values.get("SCM_HEALING_PROTECTED_ROLES", "postgres").split(",")
        )
        return cls(
            database_dsn=values.get("SCM_DB_DSN", _DEFAULT_DSN),
            mode=mode,
            policy=RulePolicy.from_env(values),
            audit_path=Path(audit_path),
            poll_interval=timedelta(seconds=_parse_int(values, "SCM_HEALING_POLL_SECONDS", 30)),
            maximum_actions_per_cycle=_parse_int(values, "SCM_HEALING_MAX_ACTIONS_PER_CYCLE", 1),
            required_consecutive_detections=_parse_int(values, "SCM_HEALING_REQUIRED_CONSECUTIVE", 2),
            vacuum_cooldown=timedelta(seconds=_parse_int(values, "SCM_HEALING_VACUUM_COOLDOWN_SECONDS", 6 * 60 * 60)),
            lock_timeout=timedelta(seconds=_parse_int(values, "SCM_HEALING_LOCK_TIMEOUT_SECONDS", 5)),
            statement_timeout=timedelta(seconds=_parse_int(values, "SCM_HEALING_STATEMENT_TIMEOUT_SECONDS", 60)),
            protected_roles=protected_roles,
            allow_terminate_idle_transaction=_parse_bool(values.get("SCM_HEALING_ALLOW_TERMINATE_IDLE_TRANSACTION", "false"), "SCM_HEALING_ALLOW_TERMINATE_IDLE_TRANSACTION"),
            allow_manual_reindex=_parse_bool(values.get("SCM_HEALING_ALLOW_MANUAL_REINDEX", "false"), "SCM_HEALING_ALLOW_MANUAL_REINDEX"),
        )

    def safe_summary(self) -> dict[str, object]:
        """Return operational settings without a DSN or other credentials."""
        return {
            "mode": self.mode.value,
            "audit_path": str(self.audit_path),
            "poll_interval_seconds": self.poll_interval.total_seconds(),
            "maximum_actions_per_cycle": self.maximum_actions_per_cycle,
            "required_consecutive_detections": self.required_consecutive_detections,
            "vacuum_cooldown_seconds": self.vacuum_cooldown.total_seconds(),
            "allow_terminate_idle_transaction": self.allow_terminate_idle_transaction,
            "allow_manual_reindex": self.allow_manual_reindex,
            "protected_roles": self.protected_roles,
        }
