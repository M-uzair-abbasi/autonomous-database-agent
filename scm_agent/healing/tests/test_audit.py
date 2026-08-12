"""Integration tests for the append-only SQLite healing audit journal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scm_agent.healing.audit import SQLiteAuditRepository
from scm_agent.healing.models import (
    ActionKind,
    ActionPlan,
    ActionResult,
    ActionStatus,
    Finding,
    Mode,
    RuleId,
    Severity,
    TargetKind,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def finding(*, fingerprint: str = "dead-tuples:17:42") -> Finding:
    return Finding(
        rule_id=RuleId.DEAD_TUPLE_PRESSURE,
        severity=Severity.WARNING,
        target_kind=TargetKind.TABLE,
        target_key="17:42",
        evidence={"n_dead_tup": 5_001, "n_live_tup": 20_000},
        recommendation="Run VACUUM on public.orders.",
        action_eligible=True,
        observed_at=NOW,
        fingerprint=fingerprint,
    )


def plan(*, mode: Mode = Mode.ACT) -> ActionPlan:
    return ActionPlan(
        action_kind=ActionKind.VACUUM,
        finding_fingerprint="dead-tuples:17:42",
        target_kind=TargetKind.TABLE,
        target_key="17:42",
        command="VACUUM (ANALYZE) public.orders",
        parameters={"relation_oid": 42},
        mode=mode,
        reason="dead tuple pressure",
    )


class SQLiteAuditRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "audit.sqlite3"
        self.repo = SQLiteAuditRepository(self.path, clock=FixedClock())
        self.target_id = self.repo.register_target(
            "postgresql://scm:secret@db.example:5434/scm_demo",
            "scm-demo postgresql://scm:secret@db.example:5434/scm_demo",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _cycle_with_event(self, *, fingerprint: str = "dead-tuples:17:42") -> tuple[int, int]:
        cycle_id = self.repo.start_cycle(self.target_id, Mode.ACT)
        event_id = self.repo.record_finding(cycle_id, finding(fingerprint=fingerprint), "tables")
        return cycle_id, event_id

    def test_schema_enables_foreign_keys_and_has_immutable_log_tables(self) -> None:
        expected = {
            "monitored_target", "poll_cycle", "cycle_transition", "anomaly_event",
            "event_transition", "action_attempt", "action_transition", "snapshot",
        }
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.record_action_intent(999_999, plan(), "unknown-event")
        with sqlite3.connect(self.path) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(expected.issubset(tables))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE monitored_target SET label = 'changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM monitored_target")

    def test_target_registration_keeps_a_sanitized_label_and_hash_not_dsn(self) -> None:
        target = self.repo.list_targets()[0]
        self.assertEqual(target["id"], self.target_id)
        self.assertEqual(target["label"], "scm-demo")
        self.assertEqual(len(target["target_hash"]), 64)
        stored = self.path.read_bytes()
        self.assertNotIn(b"postgresql://scm:secret", stored)
        self.assertNotIn(b"db.example", stored)

    def test_first_detection_then_repeat_appends_still_present_and_counts_consecutively(self) -> None:
        first_cycle, event_id = self._cycle_with_event()
        self.repo.finish_cycle(first_cycle, {"tables": True})
        second_cycle = self.repo.start_cycle(self.target_id, Mode.OBSERVE)
        same_event = self.repo.record_finding(second_cycle, finding(), "tables")
        self.repo.finish_cycle(second_cycle, {"tables": True})

        self.assertEqual(same_event, event_id)
        self.assertEqual(self.repo.consecutive_detection_count(event_id), 2)
        self.assertEqual(
            [transition["state"] for transition in self.repo.list_events(self.target_id)[0]["transitions"]],
            ["detected", "still_present"],
        )

    def test_failed_dependent_collector_does_not_clear_an_event(self) -> None:
        cycle_id, event_id = self._cycle_with_event()
        self.repo.finish_cycle(cycle_id, {"tables": True, "activities": True})
        next_cycle = self.repo.start_cycle(self.target_id, Mode.OBSERVE)
        self.repo.finish_cycle(next_cycle, {"tables": True, "activities": False})

        self.assertEqual(self.repo.list_events(self.target_id)[0]["id"], event_id)
        self.assertEqual(self.repo.list_events(self.target_id)[0]["state"], "detected")

    def test_successfully_observed_absence_appends_cleared(self) -> None:
        cycle_id, _ = self._cycle_with_event()
        self.repo.finish_cycle(cycle_id, {"tables": True})
        next_cycle = self.repo.start_cycle(self.target_id, Mode.OBSERVE)
        self.repo.finish_cycle(next_cycle, {"tables": True})

        self.assertEqual(self.repo.list_events(self.target_id)[0]["state"], "cleared")

    def test_dry_run_does_not_consume_cooldown_but_successful_real_action_does(self) -> None:
        _, event_id = self._cycle_with_event()
        dry_attempt = self.repo.record_action_intent(event_id, plan(mode=Mode.DRY_RUN), "dry-1")
        self.assertIsNotNone(dry_attempt)
        self.assertIsNone(self.repo.successful_action_at(self.target_id, ActionKind.VACUUM, "17:42"))

        real_attempt = self.repo.record_action_intent(event_id, plan(), "real-1")
        assert real_attempt is not None
        self.repo.record_action_result(
            real_attempt,
            ActionResult(ActionKind.VACUUM, ActionStatus.EXECUTING, "17:42", NOW),
        )
        self.repo.record_action_result(
            real_attempt,
            ActionResult(
                ActionKind.VACUUM, ActionStatus.SUCCEEDED, "17:42", NOW,
                completed_at=NOW + timedelta(seconds=2),
            ),
        )

        self.assertEqual(
            self.repo.successful_action_at(self.target_id, ActionKind.VACUUM, "17:42"), NOW + timedelta(seconds=2)
        )

    def test_intent_is_idempotent_and_records_before_execution(self) -> None:
        _, event_id = self._cycle_with_event()
        action_id = self.repo.record_action_intent(event_id, plan(), "unique-key")
        duplicate = self.repo.record_action_intent(event_id, plan(), "unique-key")

        self.assertIsNotNone(action_id)
        self.assertIsNone(duplicate)
        self.assertEqual(self.repo.list_actions(self.target_id)[0]["state"], "planned")

    def test_before_and_after_snapshots_are_ordered_and_json_is_canonical(self) -> None:
        _, event_id = self._cycle_with_event()
        action_id = self.repo.record_action_intent(event_id, plan(), "snapshots")
        assert action_id is not None
        self.repo.record_snapshot(action_id, "before", {"z": 1, "a": 2})
        self.repo.record_snapshot(action_id, "after", {"dead": 0})

        snapshots = self.repo.list_actions(self.target_id)[0]["snapshots"]
        self.assertEqual([snapshot["stage"] for snapshot in snapshots], ["before", "after"])
        self.assertEqual(snapshots[0]["data_json"], '{"a":2,"z":1}')

    def test_recovery_marks_an_executing_attempt_outcome_unknown_without_retrying(self) -> None:
        _, event_id = self._cycle_with_event()
        action_id = self.repo.record_action_intent(event_id, plan(), "interrupted")
        assert action_id is not None
        self.repo.record_action_result(
            action_id, ActionResult(ActionKind.VACUUM, ActionStatus.EXECUTING, "17:42", NOW)
        )
        reopened = SQLiteAuditRepository(self.path, clock=FixedClock())

        self.assertEqual(reopened.reconcile_orphans(), [action_id])
        self.assertEqual(reopened.list_actions(self.target_id)[0]["state"], "outcome_unknown")

    def test_concurrent_repositories_only_one_acquires_an_idempotency_lease(self) -> None:
        _, event_id = self._cycle_with_event()
        other = SQLiteAuditRepository(self.path, clock=FixedClock())

        first = self.repo.record_action_intent(event_id, plan(), "leased-key")
        second = other.record_action_intent(event_id, plan(), "leased-key")

        self.assertIsNotNone(first)
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
