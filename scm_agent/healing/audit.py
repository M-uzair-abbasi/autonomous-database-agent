"""Append-only SQLite audit journal for PostgreSQL healing operations.

The monitored PostgreSQL target is never used to store this information.  In
particular, callers must write an intent here before invoking an external
maintenance command, so a process crash leaves a durable, reconcilable record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .models import ActionKind, ActionPlan, ActionResult, Finding, Mode


Clock = Callable[[], datetime]
_DSN_PATTERN = re.compile(r"(?:[a-z][a-z0-9+.-]*://)\S+", re.IGNORECASE)
_CONNINFO_PATTERN = re.compile(r"\b(?:host|hostaddr|port|dbname|user|username|password|pass|pwd)\s*=", re.IGNORECASE)
_COMMAND_TOKEN_PATTERN = re.compile(
    r"\b(?:abort|alter|analyze|begin|call|checkpoint|close|cluster|comment|commit|copy|create|"
    r"deallocate|declare|delete|discard|do|drop|end|execute|explain|fetch|grant|import|insert|listen|"
    r"load|lock|merge|move|notify|prepare|reassign|refresh|reindex|release|reset|revoke|rollback|"
    r"savepoint|security|select|set|show|start|truncate|unlisten|update|vacuum|values|with)\b",
    re.IGNORECASE,
)
_SQL_MARKER_PATTERN = re.compile(r"--|/\*|\*/|;|\$\$|\$[a-z_][a-z0-9_]*\$", re.IGNORECASE)
_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|api[_-]?token|access[_-]?key|"
    r"private[_-]?key|credential|credentials)\b",
    re.IGNORECASE,
)
_IDENTIFIER = r'(?:[a-z_][a-z0-9_$]*|"(?:[^"]|"")+")'
_QUALIFIED_IDENTIFIER = rf"{_IDENTIFIER}\.{_IDENTIFIER}"
_VACUUM_COMMAND = re.compile(
    rf"vacuum\s+\(\s*skip_locked\s+true\s*,\s*truncate\s+false\s*\)\s+{_QUALIFIED_IDENTIFIER}",
    re.IGNORECASE,
)
_REINDEX_COMMAND = re.compile(
    rf"reindex\s+index\s+concurrently\s+{_QUALIFIED_IDENTIFIER}", re.IGNORECASE
)
_TERMINATE_COMMAND = re.compile(r"select\s+pg_terminate_backend\s*\(\s*\?\s*\)", re.IGNORECASE)
_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9._ -]+")
_MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ActionIntentAcquisition:
    """The atomically determined disposition of an action-intent request."""

    status: str
    action_id: int | None = None
    cooldown_until: datetime | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _is_safe_healing_command(value: str) -> bool:
    normalized = value.strip()
    return any(pattern.fullmatch(normalized) is not None for pattern in (
        _VACUUM_COMMAND, _REINDEX_COMMAND, _TERMINATE_COMMAND,
    ))


def _sanitize_text(value: str, key: str | None = None) -> str:
    """Apply a deliberately conservative lexical policy before durable writes."""
    normalized_key = (key or "").lower()
    cleaned = value.replace("\x00", " ")
    if normalized_key == "command" and _is_safe_healing_command(cleaned):
        return cleaned[:_MAX_ERROR_LENGTH]
    if _DSN_PATTERN.search(cleaned) or _CONNINFO_PATTERN.search(cleaned) or _CREDENTIAL_PATTERN.search(cleaned):
        return "[redacted-secret]"
    if "query" in normalized_key or _COMMAND_TOKEN_PATTERN.search(cleaned) or _SQL_MARKER_PATTERN.search(cleaned):
        return "[redacted-query]"
    return cleaned[:_MAX_ERROR_LENGTH]


def _json_safe(value: object, key: str | None = None) -> object:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value), key)
    if isinstance(value, Mapping):
        return {
            _sanitize_text(str(item_key), "field_name"): _json_safe(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_json_safe(item, key) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, key)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise TypeError(f"{type(value).__name__} is not JSON-safe")


def _json(value: object) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sanitize_label(value: str, target_hash: str) -> str:
    sanitized = _sanitize_text(_DSN_PATTERN.sub("", value), "label")
    cleaned = "" if sanitized.startswith("[redacted-") else _SAFE_LABEL.sub("", sanitized).strip()
    return (cleaned[:120] or f"target-{target_hash[:12]}")


def _sanitize_error(value: str | None) -> str | None:
    if value is None:
        return None
    return _sanitize_text(value, "error")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitored_target (
    id INTEGER PRIMARY KEY,
    target_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poll_cycle (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES monitored_target(id),
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycle_transition (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES poll_cycle(id),
    state TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anomaly_event (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES monitored_target(id),
    fingerprint TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    severity TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    collector TEXT NOT NULL,
    finding_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(target_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS event_transition (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES anomaly_event(id),
    cycle_id INTEGER NOT NULL REFERENCES poll_cycle(id),
    state TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS event_transition_one_per_event_cycle
ON event_transition(event_id, cycle_id);
CREATE TABLE IF NOT EXISTS action_attempt (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES anomaly_event(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    action_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    command TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    intended_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_transition (
    id INTEGER PRIMARY KEY,
    action_attempt_id INTEGER NOT NULL REFERENCES action_attempt(id),
    state TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    error TEXT,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY,
    action_attempt_id INTEGER NOT NULL REFERENCES action_attempt(id),
    stage TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
"""


class SQLiteAuditRepository:
    """A small, transactional, append-only audit repository.

    Each operation opens its own short-lived connection.  This avoids retaining
    a SQLite transaction while an engine executes a PostgreSQL command.
    """

    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or _utc_now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._install_immutable_triggers(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _install_immutable_triggers(connection: sqlite3.Connection) -> None:
        for table in (
            "monitored_target", "poll_cycle", "cycle_transition", "anomaly_event",
            "event_transition", "action_attempt", "action_transition", "snapshot",
        ):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
                f"BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only audit table'); END"
            )
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
                f"BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only audit table'); END"
            )

    def _now(self) -> str:
        return _iso(self.clock())

    def register_target(self, dsn: str, label: str) -> int:
        if not dsn.strip():
            raise ValueError("dsn must not be empty")
        target_hash = hashlib.sha256(dsn.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM monitored_target WHERE target_hash = ?", (target_hash,)
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                "INSERT INTO monitored_target(target_hash, label, registered_at) VALUES (?, ?, ?)",
                (target_hash, _sanitize_label(label, target_hash), self._now()),
            )
            return int(cursor.lastrowid)

    def list_targets(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id, label, target_hash, registered_at FROM monitored_target ORDER BY id"
            )]

    def start_cycle(self, target_id: int, mode: Mode) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO poll_cycle(target_id, mode, started_at) VALUES (?, ?, ?)",
                (target_id, mode.value, self._now()),
            )
            cycle_id = int(cursor.lastrowid)
            self._insert_cycle_transition(connection, cycle_id, "started", {})
            return cycle_id

    def _insert_cycle_transition(
        self, connection: sqlite3.Connection, cycle_id: int, state: str, detail: Mapping[str, object]
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO cycle_transition(cycle_id, state, occurred_at, detail_json) VALUES (?, ?, ?, ?)",
            (cycle_id, _sanitize_text(state, "state"), self._now(), _json(detail)),
        )
        return int(cursor.lastrowid)

    def record_cycle_transition(self, cycle_id: int, state: str, detail: Mapping[str, object] | None = None) -> int:
        with self._connect() as connection:
            return self._insert_cycle_transition(connection, cycle_id, state, detail or {})

    def record_finding(self, cycle_id: int, item: Finding, collector: str) -> int:
        if not collector.strip():
            raise ValueError("collector must not be empty")
        safe_collector = _sanitize_text(collector, "collector")
        safe_fingerprint = _sanitize_text(item.fingerprint, "fingerprint")
        safe_target_key = _sanitize_text(item.target_key, "target_key")
        finding_data = _json(item)
        with self._connect() as connection:
            target = connection.execute("SELECT target_id FROM poll_cycle WHERE id = ?", (cycle_id,)).fetchone()
            if target is None:
                raise ValueError("unknown poll cycle")
            event = connection.execute(
                "SELECT id FROM anomaly_event WHERE target_id = ? AND fingerprint = ?",
                (target["target_id"], safe_fingerprint),
            ).fetchone()
            if event is None:
                cursor = connection.execute(
                    """INSERT INTO anomaly_event(
                        target_id, fingerprint, rule_id, rule_version, severity, target_kind,
                        target_key, collector, finding_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        target["target_id"], safe_fingerprint, item.rule_id.value, _sanitize_text(item.rule_version, "rule_version"),
                        item.severity.value, item.target_kind.value, safe_target_key, safe_collector,
                        finding_data, self._now(),
                    ),
                )
                event_id = int(cursor.lastrowid)
                state = "detected"
            else:
                event_id = int(event["id"])
                latest = self._event_state(connection, event_id)
                state = "detected" if latest == "cleared" else "still_present"
            already_recorded = connection.execute(
                "SELECT 1 FROM event_transition WHERE event_id = ? AND cycle_id = ?", (event_id, cycle_id)
            ).fetchone()
            if already_recorded is None:
                self._insert_event_transition(connection, event_id, cycle_id, state, {"collector": safe_collector})
            return event_id

    def _insert_event_transition(
        self, connection: sqlite3.Connection, event_id: int, cycle_id: int, state: str, detail: Mapping[str, object]
    ) -> int:
        cursor = connection.execute(
            """INSERT INTO event_transition(event_id, cycle_id, state, occurred_at, detail_json)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, cycle_id, state, self._now(), _json(detail)),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _event_state(connection: sqlite3.Connection, event_id: int) -> str | None:
        row = connection.execute(
            "SELECT state FROM event_transition WHERE event_id = ? ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        return None if row is None else str(row["state"])

    def finish_cycle(self, cycle_id: int, collector_statuses: Mapping[str, bool]) -> int:
        """Finish a cycle and clear absent events only after every collector succeeded."""
        with self._connect() as connection:
            cycle = connection.execute("SELECT target_id FROM poll_cycle WHERE id = ?", (cycle_id,)).fetchone()
            if cycle is None:
                raise ValueError("unknown poll cycle")
            events = connection.execute(
                """SELECT event.id, event.collector FROM anomaly_event AS event
                   WHERE event.target_id = ?
                   AND COALESCE((SELECT state FROM event_transition
                                 WHERE event_id = event.id ORDER BY occurred_at DESC, id DESC LIMIT 1), '') != 'cleared'
                   AND NOT EXISTS (SELECT 1 FROM event_transition current
                                   WHERE current.event_id = event.id AND current.cycle_id = ?
                                     AND current.state IN ('detected', 'still_present'))
                   ORDER BY event.id""",
                (cycle["target_id"], cycle_id),
            ).fetchall()
            for event in events:
                required_collector = str(event["collector"])
                if collector_statuses.get(required_collector) is True:
                    self._insert_event_transition(connection, int(event["id"]), cycle_id, "cleared", {
                        "collector": required_collector,
                    })
            return self._insert_cycle_transition(connection, cycle_id, "completed", {
                "collectors": dict(collector_statuses),
            })

    def consecutive_detection_count(self, event_id: int) -> int:
        with self._connect() as connection:
            last_clear = connection.execute(
                "SELECT id FROM event_transition WHERE event_id = ? AND state = 'cleared' ORDER BY occurred_at DESC, id DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            after_transition = 0 if last_clear is None else int(last_clear["id"])
            row = connection.execute(
                """SELECT COUNT(DISTINCT cycle_id) AS count FROM event_transition
                   WHERE event_id = ? AND id > ? AND state IN ('detected', 'still_present')""",
                (event_id, after_transition),
            ).fetchone()
        return int(row["count"])

    def acquire_action_intent(
        self,
        event_id: int,
        item: ActionPlan,
        idempotency_key: str,
        cooldown: timedelta | None,
    ) -> ActionIntentAcquisition:
        """Atomically check ACT cooldown/leases and durably record an intent.

        ``acquired`` is the only disposition that authorizes a caller to run an
        external PostgreSQL action. ``duplicate`` covers an idempotency replay
        or a still-active action/target lease; ``cooldown`` includes its expiry.
        """
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if cooldown is not None and cooldown.total_seconds() < 0:
            raise ValueError("cooldown must not be negative")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT target_id FROM anomaly_event WHERE id = ?", (event_id,)
            ).fetchone()
            if event is None:
                raise ValueError("unknown anomaly event")
            safe_key = _sanitize_text(idempotency_key, "idempotency_key")
            existing_key = connection.execute(
                "SELECT id FROM action_attempt WHERE idempotency_key = ?", (safe_key,)
            ).fetchone()
            if existing_key is not None:
                connection.commit()
                return ActionIntentAcquisition("duplicate")
            if item.mode is Mode.ACT:
                active = connection.execute(
                    """SELECT attempt.id FROM action_attempt AS attempt
                       JOIN anomaly_event AS event ON event.id = attempt.event_id
                       WHERE event.target_id = ? AND attempt.action_kind = ? AND attempt.target_key = ?
                         AND attempt.mode = 'act'
                         AND (SELECT state FROM action_transition
                              WHERE action_attempt_id = attempt.id ORDER BY occurred_at DESC, id DESC LIMIT 1)
                             IN ('planned', 'executing')
                       ORDER BY attempt.id LIMIT 1""",
                    (event["target_id"], item.action_kind.value, _sanitize_text(item.target_key, "target_key")),
                ).fetchone()
                if active is not None:
                    connection.commit()
                    return ActionIntentAcquisition("duplicate")
                if cooldown is not None:
                    succeeded = connection.execute(
                        """SELECT transition.occurred_at FROM action_transition AS transition
                           JOIN action_attempt AS attempt ON attempt.id = transition.action_attempt_id
                           JOIN anomaly_event AS prior_event ON prior_event.id = attempt.event_id
                           WHERE prior_event.target_id = ? AND attempt.action_kind = ? AND attempt.target_key = ?
                             AND attempt.mode = 'act' AND transition.state = 'succeeded'
                           ORDER BY transition.occurred_at DESC, transition.id DESC LIMIT 1""",
                        (event["target_id"], item.action_kind.value, _sanitize_text(item.target_key, "target_key")),
                    ).fetchone()
                    if succeeded is not None:
                        cooldown_until = datetime.fromisoformat(str(succeeded["occurred_at"])) + cooldown
                        if self.clock() < cooldown_until:
                            connection.commit()
                            return ActionIntentAcquisition("cooldown", cooldown_until=cooldown_until)
            cursor = connection.execute(
                """INSERT INTO action_attempt(
                    event_id, idempotency_key, action_kind, target_key, mode, command, plan_json, intended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, safe_key, item.action_kind.value, _sanitize_text(item.target_key, "target_key"), item.mode.value,
                    _sanitize_text(item.command, "command"), _json(item), self._now(),
                ),
            )
            action_id = int(cursor.lastrowid)
            self._insert_action_transition(
                connection, action_id, "simulated" if item.mode is Mode.DRY_RUN else "planned", None, {}
            )
            connection.commit()
            return ActionIntentAcquisition("acquired", action_id=action_id)
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "idempotency_key" in str(exc):
                return ActionIntentAcquisition("duplicate")
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_action_intent(self, event_id: int, item: ActionPlan, idempotency_key: str) -> int | None:
        """Compatibility wrapper for callers that only need an acquired ID."""
        acquisition = self.acquire_action_intent(event_id, item, idempotency_key, None)
        return acquisition.action_id if acquisition.status == "acquired" else None

    def _insert_action_transition(
        self,
        connection: sqlite3.Connection,
        action_attempt_id: int,
        state: str,
        error: str | None,
        detail: Mapping[str, object],
        occurred_at: str | None = None,
    ) -> int:
        cursor = connection.execute(
            """INSERT INTO action_transition(action_attempt_id, state, occurred_at, error, detail_json)
               VALUES (?, ?, ?, ?, ?)""",
            (action_attempt_id, state, occurred_at or self._now(), _sanitize_error(error), _json(detail)),
        )
        return int(cursor.lastrowid)

    def record_action_result(self, action_attempt_id: int, result: ActionResult) -> int:
        occurred = result.completed_at if result.completed_at is not None else result.started_at
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT action_kind, target_key, mode FROM action_attempt WHERE id = ?", (action_attempt_id,)
            ).fetchone()
            if attempt is None:
                raise ValueError("unknown action attempt")
            if result.action_kind.value != attempt["action_kind"] or _sanitize_text(result.target_key, "target_key") != attempt["target_key"]:
                raise ValueError("action result does not match the recorded attempt")
            if attempt["mode"] == Mode.DRY_RUN.value:
                raise ValueError("dry_run attempts are simulated and cannot execute")
            latest = connection.execute(
                "SELECT state FROM action_transition WHERE action_attempt_id = ? ORDER BY occurred_at DESC, id DESC LIMIT 1",
                (action_attempt_id,),
            ).fetchone()
            previous = None if latest is None else str(latest["state"])
            allowed = {
                "planned": {"executing", "precondition_rejected", "failed"},
                "executing": {"succeeded", "failed", "outcome_unknown"},
                "succeeded": {"verified", "verification_failed"},
            }
            if result.status.value not in allowed.get(previous, set()):
                raise ValueError(f"illegal action transition from {previous!r} to {result.status.value!r}")
            transition_id = self._insert_action_transition(
                connection, action_attempt_id, result.status.value, result.error, result.evidence, _iso(occurred)
            )
            connection.commit()
            return transition_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_snapshot(self, action_attempt_id: int, stage: str, data: Mapping[str, object]) -> int:
        if stage not in {"before", "after"}:
            raise ValueError("snapshot stage must be before or after")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO snapshot(action_attempt_id, stage, captured_at, data_json) VALUES (?, ?, ?, ?)",
                (action_attempt_id, stage, self._now(), _json(data)),
            )
            return int(cursor.lastrowid)

    def successful_action_at(self, target_id: int, action_kind: ActionKind, target_key: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT transition.occurred_at FROM action_transition AS transition
                   JOIN action_attempt AS attempt ON attempt.id = transition.action_attempt_id
                   JOIN anomaly_event AS event ON event.id = attempt.event_id
                   WHERE event.target_id = ? AND attempt.action_kind = ? AND attempt.target_key = ? AND attempt.mode = 'act'
                     AND transition.state = 'succeeded'
                   ORDER BY transition.occurred_at DESC, transition.id DESC LIMIT 1""",
                (target_id, action_kind.value, _sanitize_text(target_key, "target_key")),
            ).fetchone()
        return None if row is None else datetime.fromisoformat(str(row["occurred_at"]))

    def list_events(self, target_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            events = connection.execute(
                "SELECT id, fingerprint, rule_id, severity, target_kind, target_key FROM anomaly_event WHERE target_id = ? ORDER BY id",
                (target_id,),
            ).fetchall()
            listed: list[dict[str, object]] = []
            for event in events:
                transitions = [dict(row) for row in connection.execute(
                    "SELECT id, state, occurred_at, detail_json FROM event_transition WHERE event_id = ? ORDER BY occurred_at, id",
                    (event["id"],),
                )]
                latest = transitions[-1]["state"] if transitions else None
                listed.append({**dict(event), "state": latest, "transitions": transitions})
            return listed

    def list_actions(self, target_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            attempts = connection.execute(
                """SELECT attempt.id, attempt.idempotency_key, attempt.action_kind, attempt.target_key, attempt.mode, attempt.command
                   FROM action_attempt AS attempt JOIN anomaly_event AS event ON event.id = attempt.event_id
                   WHERE event.target_id = ? ORDER BY attempt.id""",
                (target_id,),
            ).fetchall()
            listed: list[dict[str, object]] = []
            for attempt in attempts:
                transitions = [dict(row) for row in connection.execute(
                    "SELECT id, state, occurred_at, error, detail_json FROM action_transition WHERE action_attempt_id = ? ORDER BY occurred_at, id",
                    (attempt["id"],),
                )]
                snapshots = [dict(row) for row in connection.execute(
                    "SELECT id, stage, captured_at, data_json FROM snapshot WHERE action_attempt_id = ? ORDER BY id",
                    (attempt["id"],),
                )]
                latest = transitions[-1]["state"] if transitions else None
                listed.append({**dict(attempt), "state": latest, "transitions": transitions, "snapshots": snapshots})
            return listed

    def reconcile_orphans(self) -> list[int]:
        """Append ``outcome_unknown`` for attempts interrupted while executing."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            executing = connection.execute(
                """SELECT attempt.id FROM action_attempt AS attempt
                   WHERE (SELECT state FROM action_transition WHERE action_attempt_id = attempt.id
                          ORDER BY occurred_at DESC, id DESC LIMIT 1) = 'executing'
                   ORDER BY attempt.id"""
            ).fetchall()
            ids = [int(row["id"]) for row in executing]
            for action_id in ids:
                self._insert_action_transition(connection, action_id, "outcome_unknown", None, {
                    "reason": "process restarted while action was executing",
                })
            connection.commit()
            return ids
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
