"""Append-only SQLite audit journal for PostgreSQL healing operations.

The monitored PostgreSQL target is never used to store this information.  In
particular, callers must write an intent here before invoking an external
maintenance command, so a process crash leaves a durable, reconcilable record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .models import ActionKind, ActionPlan, ActionResult, Finding, Mode


Clock = Callable[[], datetime]
_DSN_PATTERN = re.compile(r"(?:[a-z][a-z0-9+.-]*://)\S+", re.IGNORECASE)
_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9._ -]+")
_MAX_ERROR_LENGTH = 500


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{type(value).__name__} is not JSON-safe")


def _json(value: object) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sanitize_label(value: str, target_hash: str) -> str:
    without_dsn = _DSN_PATTERN.sub("", value).strip(" -:/")
    cleaned = _SAFE_LABEL.sub("", without_dsn).strip()
    return (cleaned[:120] or f"target-{target_hash[:12]}")


def _sanitize_error(value: str | None) -> str | None:
    if value is None:
        return None
    return _DSN_PATTERN.sub("[redacted-dsn]", value).replace("\x00", " ")[:_MAX_ERROR_LENGTH]


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
            (cycle_id, state, self._now(), _json(detail)),
        )
        return int(cursor.lastrowid)

    def record_cycle_transition(self, cycle_id: int, state: str, detail: Mapping[str, object] | None = None) -> int:
        with self._connect() as connection:
            return self._insert_cycle_transition(connection, cycle_id, state, detail or {})

    def record_finding(self, cycle_id: int, item: Finding, collector: str) -> int:
        if not collector.strip():
            raise ValueError("collector must not be empty")
        finding_data = _json(item)
        with self._connect() as connection:
            target = connection.execute("SELECT target_id FROM poll_cycle WHERE id = ?", (cycle_id,)).fetchone()
            if target is None:
                raise ValueError("unknown poll cycle")
            event = connection.execute(
                "SELECT id FROM anomaly_event WHERE target_id = ? AND fingerprint = ?",
                (target["target_id"], item.fingerprint),
            ).fetchone()
            if event is None:
                cursor = connection.execute(
                    """INSERT INTO anomaly_event(
                        target_id, fingerprint, rule_id, rule_version, severity, target_kind,
                        target_key, collector, finding_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        target["target_id"], item.fingerprint, item.rule_id.value, item.rule_version,
                        item.severity.value, item.target_kind.value, item.target_key, collector,
                        finding_data, self._now(),
                    ),
                )
                event_id = int(cursor.lastrowid)
                state = "detected"
            else:
                event_id = int(event["id"])
                latest = self._event_state(connection, event_id)
                state = "detected" if latest == "cleared" else "still_present"
            self._insert_event_transition(connection, event_id, cycle_id, state, {"collector": collector})
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
            all_collectors_succeeded = bool(collector_statuses) and all(collector_statuses.values())
            if all_collectors_succeeded:
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
                    self._insert_event_transition(connection, int(event["id"]), cycle_id, "cleared", {
                        "collector": event["collector"],
                    })
            return self._insert_cycle_transition(connection, cycle_id, "completed", {
                "collectors": dict(collector_statuses),
            })

    def consecutive_detection_count(self, event_id: int) -> int:
        with self._connect() as connection:
            states = [str(row["state"]) for row in connection.execute(
                "SELECT state FROM event_transition WHERE event_id = ? ORDER BY occurred_at DESC, id DESC", (event_id,)
            )]
        count = 0
        for state in states:
            if state == "cleared":
                break
            if state in {"detected", "still_present"}:
                count += 1
        return count

    def record_action_intent(self, event_id: int, item: ActionPlan, idempotency_key: str) -> int | None:
        """Durably acquire an action lease and commit it before returning its id."""
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT INTO action_attempt(
                    event_id, idempotency_key, action_kind, target_key, mode, command, plan_json, intended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, idempotency_key, item.action_kind.value, item.target_key, item.mode.value,
                    item.command, _json(item), self._now(),
                ),
            )
            action_id = int(cursor.lastrowid)
            self._insert_action_transition(
                connection, action_id, "simulated" if item.mode is Mode.DRY_RUN else "planned", None, {}
            )
            connection.commit()
            return action_id
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "idempotency_key" in str(exc):
                return None
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

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
        with self._connect() as connection:
            return self._insert_action_transition(
                connection, action_attempt_id, result.status.value, result.error, result.evidence, _iso(occurred)
            )

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
                   WHERE event.target_id = ? AND attempt.action_kind = ? AND attempt.target_key = ?
                     AND transition.state = 'succeeded'
                   ORDER BY transition.occurred_at DESC, transition.id DESC LIMIT 1""",
                (target_id, action_kind.value, target_key),
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
        with self._connect() as connection:
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
            return ids
