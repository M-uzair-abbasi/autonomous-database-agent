"""Integration tests for the append-only SQLite healing audit journal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
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
        with self.repo._connect() as journal_connection:
            self.assertTrue(journal_connection.execute("PRAGMA foreign_keys").fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                journal_connection.execute(
                    """INSERT INTO action_attempt(
                        event_id, idempotency_key, action_kind, target_key, mode, command, plan_json, intended_at
                    ) VALUES (999999, 'bad-fk', 'vacuum', '17:42', 'act', 'VACUUM', '{}', '2026-08-12T12:00:00+00:00')"""
                )
        _, event_id = self._cycle_with_event()
        action_id = self.repo.record_action_intent(event_id, plan(), "immutable-rows")
        assert action_id is not None
        self.repo.record_snapshot(action_id, "before", {"n_dead_tup": 5_001})
        with sqlite3.connect(self.path) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(expected.issubset(tables))
            for table in expected:
                with self.subTest(table=table, operation="update"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(f"UPDATE {table} SET id = id")
                with self.subTest(table=table, operation="delete"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(f"DELETE FROM {table} WHERE id = (SELECT id FROM {table} LIMIT 1)")

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
        self.repo.finish_cycle(next_cycle, {"tables": False, "activities": True})

        self.assertEqual(self.repo.list_events(self.target_id)[0]["id"], event_id)
        self.assertEqual(self.repo.list_events(self.target_id)[0]["state"], "detected")

    def test_missing_required_collector_status_never_clears_its_event(self) -> None:
        cycle_id, _ = self._cycle_with_event()
        self.repo.finish_cycle(cycle_id, {"tables": True})
        next_cycle = self.repo.start_cycle(self.target_id, Mode.OBSERVE)
        self.repo.finish_cycle(next_cycle, {"activities": True})

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

    def test_finding_is_idempotent_within_a_cycle_and_counts_distinct_cycles(self) -> None:
        cycle_id, event_id = self._cycle_with_event()
        self.assertEqual(self.repo.record_finding(cycle_id, finding(), "tables"), event_id)

        self.assertEqual(self.repo.consecutive_detection_count(event_id), 1)
        self.assertEqual(len(self.repo.list_events(self.target_id)[0]["transitions"]), 1)

    def test_atomic_acquisition_checks_cooldown_and_active_target_lease(self) -> None:
        _, event_id = self._cycle_with_event()
        acquired = self.repo.acquire_action_intent(event_id, plan(), "cycle-one", timedelta(hours=6))
        duplicate = self.repo.acquire_action_intent(event_id, plan(), "cycle-two", timedelta(hours=6))

        self.assertEqual(acquired.status, "acquired")
        self.assertIsNotNone(acquired.action_id)
        self.assertEqual(duplicate.status, "duplicate")
        self.assertIsNone(duplicate.action_id)

        assert acquired.action_id is not None
        self.repo.record_action_result(
            acquired.action_id,
            ActionResult(ActionKind.VACUUM, ActionStatus.EXECUTING, "17:42", NOW),
        )
        self.repo.record_action_result(
            acquired.action_id,
            ActionResult(ActionKind.VACUUM, ActionStatus.SUCCEEDED, "17:42", NOW, NOW + timedelta(seconds=2)),
        )
        cooldown = self.repo.acquire_action_intent(event_id, plan(), "cycle-three", timedelta(hours=6))

        self.assertEqual(cooldown.status, "cooldown")
        self.assertEqual(cooldown.cooldown_until, NOW + timedelta(hours=6, seconds=2))

    def test_dry_run_cannot_transition_to_execution_or_success(self) -> None:
        _, event_id = self._cycle_with_event()
        action_id = self.repo.record_action_intent(event_id, plan(mode=Mode.DRY_RUN), "dry-state")
        assert action_id is not None

        with self.assertRaisesRegex(ValueError, "dry_run"):
            self.repo.record_action_result(
                action_id, ActionResult(ActionKind.VACUUM, ActionStatus.EXECUTING, "17:42", NOW)
            )
        with self.assertRaisesRegex(ValueError, "dry_run"):
            self.repo.record_action_result(
                action_id, ActionResult(ActionKind.VACUUM, ActionStatus.SUCCEEDED, "17:42", NOW, NOW)
            )

    def test_action_result_rejects_mismatched_identity_and_illegal_transition(self) -> None:
        _, event_id = self._cycle_with_event()
        action_id = self.repo.record_action_intent(event_id, plan(), "state-machine")
        assert action_id is not None

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.repo.record_action_result(
                action_id, ActionResult(ActionKind.REINDEX_INDEX, ActionStatus.EXECUTING, "17:42", NOW)
            )
        with self.assertRaisesRegex(ValueError, "illegal"):
            self.repo.record_action_result(
                action_id, ActionResult(ActionKind.VACUUM, ActionStatus.SUCCEEDED, "17:42", NOW, NOW)
            )

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

    def test_concurrent_repositories_only_one_acquires_an_action_target_lease(self) -> None:
        _, event_id = self._cycle_with_event()
        other = SQLiteAuditRepository(self.path, clock=FixedClock())
        barrier = threading.Barrier(2)
        results = []

        def acquire(repository: SQLiteAuditRepository, key: str) -> None:
            barrier.wait()
            results.append(repository.acquire_action_intent(event_id, plan(), key, timedelta(hours=6)))

        threads = [
            threading.Thread(target=acquire, args=(self.repo, "leased-cycle-one")),
            threading.Thread(target=acquire, args=(other, "leased-cycle-two")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(result.status for result in results), ["acquired", "duplicate"])
        self.assertEqual(len(self.repo.list_actions(self.target_id)), 1)

    def test_concurrent_orphan_recovery_appends_one_terminal_transition(self) -> None:
        _, event_id = self._cycle_with_event()
        action_id = self.repo.record_action_intent(event_id, plan(), "contended-recovery")
        assert action_id is not None
        self.repo.record_action_result(
            action_id, ActionResult(ActionKind.VACUUM, ActionStatus.EXECUTING, "17:42", NOW)
        )
        first = SQLiteAuditRepository(self.path, clock=FixedClock())
        second = SQLiteAuditRepository(self.path, clock=FixedClock())
        barrier = threading.Barrier(2)
        results: list[list[int]] = []

        def recover(repository: SQLiteAuditRepository) -> None:
            barrier.wait()
            results.append(repository.reconcile_orphans())

        threads = [threading.Thread(target=recover, args=(repository,)) for repository in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), [[], [action_id]])
        transitions = self.repo.list_actions(self.target_id)[0]["transitions"]
        self.assertEqual([transition["state"] for transition in transitions], ["planned", "executing", "outcome_unknown"])

    def test_sensitive_values_and_raw_query_text_are_never_persisted(self) -> None:
        uri = "postgresql://alice:supersecret@hidden.example/private"
        conninfo = "host=hidden.example user=alice password=supersecret dbname=private"
        query = "SELECT credit_card FROM customers"
        cycle_id = self.repo.start_cycle(self.target_id, Mode.ACT)
        event_id = self.repo.record_finding(
            cycle_id,
            Finding(
                rule_id=RuleId.DEAD_TUPLE_PRESSURE,
                severity=Severity.WARNING,
                target_kind=TargetKind.TABLE,
                target_key="17:42",
                evidence={"dsn": uri, "conninfo": conninfo, "query_text": query},
                recommendation=query,
                action_eligible=True,
                observed_at=NOW,
                fingerprint="secret-finding",
            ),
            "tables",
        )
        self.repo.record_cycle_transition(cycle_id, "collector_failed", {"dsn": uri, "query": query})
        secret_plan = ActionPlan(
            action_kind=ActionKind.VACUUM,
            finding_fingerprint="secret-finding",
            target_kind=TargetKind.TABLE,
            target_key="17:42",
            command=query,
            parameters={"connection": conninfo, "query": query},
            mode=Mode.ACT,
            reason=query,
        )
        action_id = self.repo.record_action_intent(event_id, secret_plan, f"lease {uri} {query}")
        assert action_id is not None
        self.repo.record_action_result(
            action_id,
            ActionResult(ActionKind.VACUUM, ActionStatus.EXECUTING, "17:42", NOW, error=f"{uri} {conninfo} {query}", evidence={"query": query}),
        )
        self.repo.record_snapshot(action_id, "before", {"credentials": conninfo, "query": query})

        stored = b"".join(
            candidate.read_bytes() for candidate in (self.path, Path(f"{self.path}-wal")) if candidate.exists()
        )
        for secret in (b"supersecret", b"hidden.example", b"SELECT credit_card", b"alice"):
            self.assertNotIn(secret, stored)


if __name__ == "__main__":
    unittest.main()
