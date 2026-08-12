"""End-to-end orchestration tests with a real SQLite journal and fake gateway."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from typing import Mapping
import unittest

from scm_agent.healing.actions import ActionPlanner
from scm_agent.healing.audit import SQLiteAuditRepository
from scm_agent.healing.config import HealingConfig
from scm_agent.healing.engine import HealingEngine
from scm_agent.healing.models import (
    ActionKind,
    ActionPlan,
    ActionResult,
    ActionStatus,
    ActivitySample,
    CollectionError,
    ConnectionSample,
    HealthSnapshot,
    IndexStatsSample,
    Mode,
    TableStatsSample,
)
from scm_agent.healing.postgres import CapabilityDecision
from scm_agent.healing.rules import detect_all


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def pressured_snapshot(*, observed_at: datetime = NOW) -> HealthSnapshot:
    return HealthSnapshot(
        observed_at=observed_at,
        tables=(
            TableStatsSample(
                observed_at=observed_at,
                database_oid=17,
                relation_oid=42,
                schema_name="public",
                relation_name="orders",
                n_live_tup=20_000,
                n_dead_tup=10_000,
            ),
        ),
    )


class FakeGateway:
    """Specific stateful fake: read operations and mutations have separate traces."""

    def __init__(
        self,
        snapshots: list[HealthSnapshot],
        *,
        decision: CapabilityDecision | None = None,
        result_status: ActionStatus = ActionStatus.SUCCEEDED,
    ) -> None:
        self.snapshots = list(snapshots)
        self.decision = decision or CapabilityDecision(
            True, "ok", evidence={"identity": {"fixture": True}}
        )
        self.result_status = result_status
        self.read_trace: list[str] = []
        self.executed: list[ActionPlan] = []

    def collect_health(self) -> HealthSnapshot:
        self.read_trace.append("collect")
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def check_plan(self, plan: ActionPlan) -> CapabilityDecision:
        self.read_trace.append(f"check:{plan.target_key}")
        return self.decision

    def capture_before(self, plan: ActionPlan) -> Mapping[str, object]:
        self.read_trace.append(f"before:{plan.target_key}")
        return {"present": True, "dead_tuples": 10_000}

    def execute(self, plan: ActionPlan) -> ActionResult:
        self.executed.append(plan)
        return ActionResult(
            plan.action_kind,
            self.result_status,
            plan.target_key,
            NOW,
            NOW + timedelta(seconds=1),
            error="safe action failure" if self.result_status is ActionStatus.FAILED else None,
        )

    def capture_after(self, plan: ActionPlan) -> Mapping[str, object]:
        self.read_trace.append(f"after:{plan.target_key}")
        return {"present": True, "dead_tuples": 0}


class EngineModeMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.audit_path = Path(self.tempdir.name) / "audit.sqlite3"
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _engine(self, mode: Mode) -> tuple[HealingEngine, SQLiteAuditRepository, FakeGateway, int]:
        repository = SQLiteAuditRepository(self.audit_path, clock=self.clock)
        gateway = FakeGateway([pressured_snapshot(), pressured_snapshot()])
        config = HealingConfig(
            database_dsn="postgresql://scm:secret@db.example:5434/scm_demo",
            mode=mode,
            audit_path=self.audit_path,
        )
        engine = HealingEngine(config, gateway, repository, clock=self.clock)
        target_id = repository.register_target(config.database_dsn, "scm-demo")
        return engine, repository, gateway, target_id

    def test_observe_persists_findings_without_plans_or_execution(self) -> None:
        engine, repository, gateway, target_id = self._engine(Mode.OBSERVE)

        first = engine.run_once()
        second = engine.run_once()

        self.assertEqual((first.finding_count, second.finding_count), (1, 1))
        self.assertEqual((first.plan_count, second.plan_count), (0, 0))
        self.assertEqual(repository.consecutive_detection_count(repository.list_events(target_id)[0]["id"]), 2)
        self.assertEqual(repository.list_actions(target_id), [])
        self.assertEqual(gateway.executed, [])

    def test_dry_run_waits_for_second_cycle_then_revalidates_and_records_simulation(self) -> None:
        engine, repository, gateway, target_id = self._engine(Mode.DRY_RUN)

        first = engine.run_once()
        second = engine.run_once()

        self.assertEqual((first.simulated_count, second.simulated_count), (0, 1))
        self.assertEqual(second.action_count, 1)
        self.assertEqual(gateway.executed, [])
        self.assertEqual(gateway.read_trace.count("check:17:42"), 1)
        actions = repository.list_actions(target_id)
        self.assertEqual([action["state"] for action in actions], ["simulated"])
        self.assertEqual(
            [snapshot["stage"] for snapshot in actions[0]["snapshots"]],
            ["before", "after"],
        )

    def test_act_waits_for_second_cycle_then_executes_the_eligible_plan(self) -> None:
        engine, repository, gateway, target_id = self._engine(Mode.ACT)

        first = engine.run_once()
        second = engine.run_once()

        self.assertEqual(first.action_count, 0)
        self.assertEqual((second.action_count, second.succeeded_count), (1, 1))
        self.assertEqual([plan.action_kind for plan in gateway.executed], [ActionKind.VACUUM])
        action = repository.list_actions(target_id)[0]
        self.assertEqual(action["state"], "succeeded")
        self.assertEqual(
            [transition["state"] for transition in action["transitions"]],
            ["planned", "executing", "succeeded"],
        )

    def test_dry_run_does_not_consume_the_real_action_cooldown(self) -> None:
        engine, repository, gateway, target_id = self._engine(Mode.DRY_RUN)

        engine.run_once()
        engine.run_once()
        third = engine.run_once()

        self.assertEqual(third.simulated_count, 1)
        self.assertEqual(len(repository.list_actions(target_id)), 2)
        self.assertEqual(gateway.executed, [])


class FailingIntentRepository(SQLiteAuditRepository):
    def acquire_action_intent(self, *args: object, **kwargs: object):
        raise OSError("audit storage unavailable")


class IntentAwareGateway(FakeGateway):
    def __init__(
        self,
        snapshots: list[HealthSnapshot],
        repository: SQLiteAuditRepository,
        target_id: int,
    ) -> None:
        super().__init__(snapshots)
        self.repository = repository
        self.target_id = target_id

    def execute(self, plan: ActionPlan) -> ActionResult:
        actions = self.repository.list_actions(self.target_id)
        if not actions or actions[-1]["state"] != "executing":
            return ActionResult(
                plan.action_kind,
                ActionStatus.FAILED,
                plan.target_key,
                NOW,
                NOW,
                error="intent not durably visible",
            )
        return super().execute(plan)


class RaisingGateway(FakeGateway):
    def __init__(self, snapshots: list[HealthSnapshot], *, stage: str) -> None:
        super().__init__(snapshots)
        self.stage = stage

    def capture_before(self, plan: ActionPlan) -> Mapping[str, object]:
        if self.stage == "before":
            raise OSError("safe before-snapshot failure")
        return super().capture_before(plan)

    def execute(self, plan: ActionPlan) -> ActionResult:
        if self.stage == "execute":
            self.executed.append(plan)
            raise OSError("safe indeterminate dispatch failure")
        return super().execute(plan)

    def capture_after(self, plan: ActionPlan) -> Mapping[str, object]:
        if self.stage == "after":
            raise OSError("safe after-snapshot failure")
        return super().capture_after(plan)


class PostActionFailingRepository(SQLiteAuditRepository):
    def __init__(self, *args: object, failure: str, repeat: bool = False, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.failure = failure
        self.repeat = repeat
        self.failures = 0
        self.cycle_terminal_attempts = 0

    def record_snapshot(
        self, action_attempt_id: int, stage: str, data: Mapping[str, object]
    ) -> int:
        if self.failure == "after_snapshot" and stage == "after" and (
            self.repeat or self.failures == 0
        ):
            self.failures += 1
            raise OSError("audit after-snapshot unavailable")
        return super().record_snapshot(action_attempt_id, stage, data)

    def record_action_result(self, action_attempt_id: int, result: ActionResult) -> int:
        if (
            self.failure == "terminal_result"
            and result.status is not ActionStatus.EXECUTING
            and (self.repeat or self.failures == 0)
        ):
            self.failures += 1
            raise OSError("audit terminal-result unavailable")
        return super().record_action_result(action_attempt_id, result)

    def finish_cycle(self, cycle_id: int, collector_statuses: Mapping[str, bool]) -> int:
        self.cycle_terminal_attempts += 1
        return super().finish_cycle(cycle_id, collector_statuses)

    def record_cycle_transition(
        self, cycle_id: int, state: str, detail: Mapping[str, object] | None = None
    ) -> int:
        self.cycle_terminal_attempts += 1
        return super().record_cycle_transition(cycle_id, state, detail)


class TransientBoundaryRepository(SQLiteAuditRepository):
    def __init__(
        self,
        *args: object,
        terminal_failures: int = 0,
        reconcile_failures: int = 0,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.terminal_failures = terminal_failures
        self.reconcile_failures = reconcile_failures
        self.reconcile_calls = 0

    def record_action_result(self, action_attempt_id: int, result: ActionResult) -> int:
        if result.status is not ActionStatus.EXECUTING and self.terminal_failures > 0:
            self.terminal_failures -= 1
            raise OSError("transient terminal audit failure")
        return super().record_action_result(action_attempt_id, result)

    def reconcile_orphans(self) -> list[int]:
        self.reconcile_calls += 1
        if self.reconcile_failures > 0:
            self.reconcile_failures -= 1
            raise OSError("transient reconciliation failure")
        return super().reconcile_orphans()


class EngineResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "audit.sqlite3"
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _config(self, mode: Mode = Mode.ACT, **changes: object) -> HealingConfig:
        values = {
            "database_dsn": "postgresql://scm:secret@db.example:5434/scm_demo",
            "mode": mode,
            "audit_path": self.path,
        }
        values.update(changes)
        return HealingConfig(**values)

    def _run_twice(
        self,
        gateway: FakeGateway,
        *,
        repository: SQLiteAuditRepository | None = None,
        config: HealingConfig | None = None,
    ) -> tuple[HealingEngine, SQLiteAuditRepository, object]:
        audit = repository or SQLiteAuditRepository(self.path, clock=self.clock)
        engine = HealingEngine(config or self._config(), gateway, audit, clock=self.clock)
        engine.run_once()
        return engine, audit, engine.run_once()

    def test_audit_failure_before_intent_results_in_zero_external_actions(self) -> None:
        repository = FailingIntentRepository(self.path, clock=self.clock)
        gateway = FakeGateway([pressured_snapshot(), pressured_snapshot()])

        _, _, summary = self._run_twice(gateway, repository=repository)

        self.assertEqual(summary.status, "failed")
        self.assertEqual(gateway.executed, [])

    def test_committed_intent_is_visible_before_gateway_execution(self) -> None:
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        config = self._config()
        target_id = repository.register_target(config.database_dsn, "fixture")
        gateway = IntentAwareGateway(
            [pressured_snapshot(), pressured_snapshot()], repository, target_id
        )

        _, _, summary = self._run_twice(gateway, repository=repository, config=config)

        self.assertEqual(summary.succeeded_count, 1)
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "succeeded")

    def test_collector_failure_does_not_clear_its_dependent_event(self) -> None:
        failed = HealthSnapshot(
            observed_at=NOW,
            errors=(CollectionError("tables", "safe collector failure", NOW),),
        )
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        gateway = FakeGateway([pressured_snapshot(), failed])
        engine = HealingEngine(self._config(Mode.OBSERVE), gateway, repository, clock=self.clock)

        engine.run_once()
        summary = engine.run_once()

        target_id = repository.list_targets()[0]["id"]
        event = repository.list_events(target_id)[0]
        self.assertEqual((summary.finding_count, summary.collector_error_count), (0, 1))
        self.assertEqual(event["state"], "detected")

    def test_failed_action_is_terminal_but_cycle_still_completes(self) -> None:
        gateway = FakeGateway(
            [pressured_snapshot(), pressured_snapshot()], result_status=ActionStatus.FAILED
        )

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual((summary.status, summary.failed_count), ("completed", 1))
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "failed")

    def test_before_snapshot_failure_prevents_execution_and_releases_the_lease(self) -> None:
        gateway = RaisingGateway([pressured_snapshot(), pressured_snapshot()], stage="before")

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual((summary.status, summary.failed_count), ("completed", 1))
        self.assertEqual(gateway.executed, [])
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "failed")

    def test_unexpected_dispatch_exception_is_audited_unknown_and_cycle_completes(self) -> None:
        gateway = RaisingGateway([pressured_snapshot(), pressured_snapshot()], stage="execute")

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual((summary.status, summary.unknown_count), ("completed", 1))
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "outcome_unknown")

    def _cycle_states(self, repository: SQLiteAuditRepository) -> list[str]:
        with repository._connect() as connection:
            return [
                str(row["state"])
                for row in connection.execute(
                    "SELECT state FROM cycle_transition ORDER BY id"
                )
            ]

    def test_after_capture_failure_preserves_result_but_fails_cycle_without_success_claim(self) -> None:
        gateway = RaisingGateway([pressured_snapshot(), pressured_snapshot()], stage="after")

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual(len(gateway.executed), 1)
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "succeeded")
        self.assertEqual(
            (summary.status, summary.succeeded_count, summary.failed_count),
            ("failed", 0, 1),
        )
        self.assertEqual(self._cycle_states(repository)[-1], "failed")

    def test_after_snapshot_audit_failure_still_persists_known_result_and_terminalizes_cycle(self) -> None:
        repository = PostActionFailingRepository(
            self.path, clock=self.clock, failure="after_snapshot"
        )
        gateway = FakeGateway([pressured_snapshot(), pressured_snapshot()])

        _, _, summary = self._run_twice(gateway, repository=repository)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual(len(gateway.executed), 1)
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "succeeded")
        self.assertEqual(
            (summary.status, summary.succeeded_count, summary.failed_count),
            ("failed", 0, 1),
        )
        self.assertGreaterEqual(repository.cycle_terminal_attempts, 2)
        self.assertEqual(self._cycle_states(repository)[-1], "failed")

    def test_terminal_result_persistence_failure_falls_back_to_unknown_and_fails_cycle(self) -> None:
        repository = PostActionFailingRepository(
            self.path, clock=self.clock, failure="terminal_result"
        )
        gateway = FakeGateway([pressured_snapshot(), pressured_snapshot()])

        _, _, summary = self._run_twice(gateway, repository=repository)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual(len(gateway.executed), 1)
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "outcome_unknown")
        self.assertEqual(
            (summary.status, summary.succeeded_count, summary.unknown_count),
            ("failed", 0, 1),
        )
        self.assertEqual(self._cycle_states(repository)[-1], "failed")

    def test_repeated_terminal_persistence_failure_leaves_explicitly_recoverable_action(self) -> None:
        repository = PostActionFailingRepository(
            self.path, clock=self.clock, failure="terminal_result", repeat=True
        )
        gateway = FakeGateway([pressured_snapshot(), pressured_snapshot()])

        _, _, summary = self._run_twice(gateway, repository=repository)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual(len(gateway.executed), 1)
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "executing")
        self.assertEqual(summary.status, "failed")
        self.assertEqual(
            SQLiteAuditRepository(self.path, clock=self.clock).reconcile_orphans(), [1]
        )

    def test_stale_precondition_is_skipped_before_intent(self) -> None:
        gateway = FakeGateway(
            [pressured_snapshot(), pressured_snapshot()],
            decision=CapabilityDecision(False, "stale_relation_identity"),
        )

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual((summary.action_count, summary.skipped_count), (0, 1))
        self.assertEqual(repository.list_actions(target_id), [])
        self.assertEqual(gateway.executed, [])

    def test_runtime_gateway_precondition_remains_the_final_gate(self) -> None:
        gateway = FakeGateway(
            [pressured_snapshot(), pressured_snapshot()],
            result_status=ActionStatus.PRECONDITION_REJECTED,
        )

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual((summary.succeeded_count, summary.skipped_count), (0, 1))
        self.assertEqual(
            repository.list_actions(target_id)[0]["state"], "precondition_rejected"
        )
        self.assertIsNone(
            repository.successful_action_at(target_id, ActionKind.VACUUM, "17:42")
        )

    def test_unknown_result_is_preserved_and_cycle_completes(self) -> None:
        gateway = FakeGateway(
            [pressured_snapshot(), pressured_snapshot()],
            result_status=ActionStatus.OUTCOME_UNKNOWN,
        )

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual((summary.status, summary.unknown_count), ("completed", 1))
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "outcome_unknown")

    def test_orphan_recovery_occurs_before_the_first_new_cycle(self) -> None:
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        gateway = FakeGateway([pressured_snapshot(), pressured_snapshot()])
        engine = HealingEngine(self._config(), gateway, repository, clock=self.clock)
        engine.run_once()
        engine.run_once()
        target_id = repository.list_targets()[0]["id"]
        action = repository.list_actions(target_id)[0]
        # Append an orphan directly using the real journal state machine.
        cycle_id = repository.start_cycle(target_id, Mode.ACT)
        finding = detect_all(pressured_snapshot(), self._config().policy)[0]
        event_id = repository.record_finding(cycle_id, finding, "tables")
        plan = ActionPlanner().plan(finding, self._config())
        assert plan is not None
        acquired = repository.acquire_action_intent(event_id, plan, "orphan-fixture", None)
        assert acquired.action_id is not None
        repository.record_action_result(
            acquired.action_id,
            ActionResult(ActionKind.VACUUM, ActionStatus.EXECUTING, "17:42", NOW),
        )
        reopened_gateway = FakeGateway([HealthSnapshot(observed_at=NOW)])
        reopened = HealingEngine(
            self._config(Mode.OBSERVE), reopened_gateway, repository, clock=self.clock
        )

        summary = reopened.run_once()

        actions = repository.list_actions(target_id)
        self.assertEqual(summary.recovered_orphan_count, 1)
        self.assertEqual(actions[-1]["state"], "outcome_unknown")
        self.assertEqual(action["state"], "succeeded")

    def test_startup_recovers_planned_intent_before_allowing_a_later_cycle_to_act(self) -> None:
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        config = self._config()
        target_id = repository.register_target(config.database_dsn, "fixture")
        cycle_id = repository.start_cycle(target_id, Mode.ACT)
        finding = detect_all(pressured_snapshot(), config.policy)[0]
        event_id = repository.record_finding(cycle_id, finding, "tables")
        plan = ActionPlanner().plan(finding, config)
        assert plan is not None
        orphan = repository.acquire_action_intent(event_id, plan, "planned-startup", None)
        assert orphan.action_id is not None
        gateway = FakeGateway([pressured_snapshot()])
        restarted = HealingEngine(config, gateway, repository, clock=self.clock)

        recovery = restarted.run_once()
        later = restarted.run_once()

        actions = repository.list_actions(target_id)
        self.assertEqual((recovery.recovered_orphan_count, recovery.action_count), (1, 0))
        self.assertEqual(later.action_count, 1)
        self.assertEqual([action["state"] for action in actions], ["outcome_unknown", "succeeded"])

    def test_action_budget_and_priority_choose_one_deterministic_target(self) -> None:
        snapshot = HealthSnapshot(
            observed_at=NOW,
            tables=(
                TableStatsSample(NOW, 17, 99, "public", "later", 20_000, 10_000),
                TableStatsSample(NOW, 17, 42, "public", "first", 20_000, 10_000),
            ),
        )
        gateway = FakeGateway([snapshot, snapshot])

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual(summary.action_count, 1)
        self.assertEqual([plan.target_key for plan in gateway.executed], ["17:42"])
        self.assertEqual(len(repository.list_actions(target_id)), 1)

    def test_defensive_engine_limits_override_an_unsafely_constructed_config(self) -> None:
        safe = self._config()
        unsafe = object.__new__(HealingConfig)
        for name, value in vars(safe).items():
            object.__setattr__(unsafe, name, value)
        object.__setattr__(unsafe, "required_consecutive_detections", 1)
        object.__setattr__(unsafe, "maximum_actions_per_cycle", 99)
        snapshot = HealthSnapshot(
            observed_at=NOW,
            tables=(
                TableStatsSample(NOW, 17, 42, "public", "first", 20_000, 10_000),
                TableStatsSample(NOW, 17, 99, "public", "second", 20_000, 10_000),
            ),
        )
        gateway = FakeGateway([snapshot])
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        engine = HealingEngine(unsafe, gateway, repository, clock=self.clock)

        first = engine.run_once()
        second = engine.run_once()

        self.assertEqual(first.action_count, 0)
        self.assertEqual(second.action_count, 1)
        self.assertEqual(len(gateway.executed), 1)

    def test_cooldown_blocked_candidate_does_not_hide_another_eligible_plan(self) -> None:
        snapshot = HealthSnapshot(
            observed_at=NOW,
            tables=(
                TableStatsSample(NOW, 17, 42, "public", "first", 20_000, 10_000),
                TableStatsSample(NOW, 17, 99, "public", "second", 20_000, 10_000),
            ),
        )
        gateway = FakeGateway([snapshot])
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        engine = HealingEngine(self._config(), gateway, repository, clock=self.clock)

        engine.run_once()
        engine.run_once()
        third = engine.run_once()

        self.assertEqual(third.action_count, 1)
        self.assertEqual([plan.target_key for plan in gateway.executed], ["17:42", "17:99"])

    def test_backend_termination_runs_once_per_exact_session_identity(self) -> None:
        backend_start = NOW - timedelta(hours=1)
        transaction_start = NOW - timedelta(minutes=30)
        state_change = NOW - timedelta(minutes=20)
        snapshot = HealthSnapshot(
            observed_at=NOW,
            activities=(
                ActivitySample(
                    NOW, 17, "scm_demo", 123, backend_start, "scm", "psql",
                    "client backend", "idle in transaction", state_change,
                    transaction_start=transaction_start,
                ),
            ),
        )
        gateway = FakeGateway([snapshot])
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        engine = HealingEngine(
            self._config(allow_terminate_idle_transaction=True),
            gateway,
            repository,
            clock=self.clock,
        )

        engine.run_once()
        second = engine.run_once()
        third = engine.run_once()

        self.assertEqual((second.action_count, third.action_count), (1, 0))
        self.assertEqual(
            [plan.action_kind for plan in gateway.executed], [ActionKind.TERMINATE_BACKEND]
        )

    def test_advisory_findings_never_create_automatic_destructive_plans(self) -> None:
        snapshot = HealthSnapshot(
            observed_at=NOW,
            activities=(
                ActivitySample(
                    NOW, 17, "scm_demo", 123, NOW - timedelta(hours=1), "scm", "psql",
                    "client backend", "active", NOW - timedelta(minutes=20),
                    query_start=NOW - timedelta(minutes=10),
                ),
            ),
            indexes=(
                IndexStatsSample(
                    NOW, 17, 42, 77, "public", "orders", "unused_idx",
                    200 * 1024 * 1024, 0, stats_reset=NOW - timedelta(days=10),
                ),
            ),
            connection=ConnectionSample(NOW, 95, 100),
        )
        gateway = FakeGateway([snapshot, snapshot])

        _, repository, summary = self._run_twice(gateway)

        target_id = repository.list_targets()[0]["id"]
        self.assertEqual(summary.finding_count, 3)
        self.assertEqual(summary.plan_count, 0)
        self.assertEqual(repository.list_actions(target_id), [])
        self.assertEqual(gateway.executed, [])

    def test_summary_exposes_only_statuses_and_integer_counts(self) -> None:
        gateway = FakeGateway([pressured_snapshot()])
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        engine = HealingEngine(self._config(Mode.OBSERVE), gateway, repository, clock=self.clock)

        summary = asdict(engine.run_once())

        self.assertEqual(set(type(value) for value in summary.values()), {str, int})
        self.assertNotIn("postgresql", repr(summary))
        self.assertNotIn("secret", repr(summary))

    def test_successful_vacuum_is_cooled_down_for_six_hours(self) -> None:
        gateway = FakeGateway([pressured_snapshot()])
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        engine = HealingEngine(self._config(), gateway, repository, clock=self.clock)

        engine.run_once()
        engine.run_once()
        blocked = engine.run_once()
        self.clock.value += timedelta(hours=6, seconds=2)
        allowed = engine.run_once()

        self.assertEqual((blocked.action_count, blocked.skipped_count), (0, 1))
        self.assertEqual((allowed.action_count, allowed.succeeded_count), (1, 1))
        self.assertEqual(len(gateway.executed), 2)

    def test_saturation_collector_error_suppresses_its_dependent_rule(self) -> None:
        snapshot = HealthSnapshot(
            observed_at=NOW,
            connection=ConnectionSample(NOW, 95, 100),
            errors=(CollectionError("saturation", "safe failure", NOW),),
        )
        gateway = FakeGateway([snapshot])
        repository = SQLiteAuditRepository(self.path, clock=self.clock)
        engine = HealingEngine(self._config(Mode.OBSERVE), gateway, repository, clock=self.clock)

        summary = engine.run_once()

        self.assertEqual(summary.finding_count, 0)
        self.assertEqual(repository.list_events(repository.list_targets()[0]["id"]), [])


class FakeMonotonicSleeper:
    def __init__(self) -> None:
        self.value = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class DaemonLoopTests(unittest.TestCase):
    @staticmethod
    def _cycle_states(repository: SQLiteAuditRepository) -> list[str]:
        with repository._connect() as connection:
            return [
                str(row["state"])
                for row in connection.execute(
                    "SELECT state FROM cycle_transition ORDER BY id"
                )
            ]

    def test_run_forever_uses_monotonic_remaining_interval_and_stops_serially(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "audit.sqlite3"
            clock = MutableClock()
            timer = FakeMonotonicSleeper()
            repository = SQLiteAuditRepository(path, clock=clock)
            gateway = FakeGateway([HealthSnapshot(observed_at=NOW)])
            config = HealingConfig(
                database_dsn="postgresql://scm:secret@db.example:5434/scm_demo",
                mode=Mode.OBSERVE,
                audit_path=path,
                poll_interval=timedelta(seconds=10),
            )
            engine = HealingEngine(
                config,
                gateway,
                repository,
                clock=clock,
                sleeper=timer.sleep,
                monotonic=timer.monotonic,
            )
            original = engine.run_once
            cycles = 0
            active = 0
            maximum_active = 0

            def timed_cycle():
                nonlocal cycles, active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                timer.value += 3.0
                result = original()
                active -= 1
                cycles += 1
                return result

            engine.run_once = timed_cycle  # type: ignore[method-assign]

            engine.run_forever(lambda: cycles >= 2)

            self.assertEqual(cycles, 2)
            self.assertEqual(maximum_active, 1)
            self.assertEqual(timer.sleeps, [7.0])

    def test_run_forever_checks_stop_before_starting_any_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "audit.sqlite3"
            repository = SQLiteAuditRepository(path, clock=MutableClock())
            gateway = FakeGateway([HealthSnapshot(observed_at=NOW)])
            engine = HealingEngine(
                HealingConfig(mode=Mode.OBSERVE, audit_path=path),
                gateway,
                repository,
                clock=MutableClock(),
                sleeper=lambda seconds: self.fail(f"slept {seconds}"),
                monotonic=lambda: 0.0,
            )

            engine.run_forever(lambda: True)

            self.assertEqual(gateway.read_trace, [])

    def test_run_forever_continues_after_one_cycle_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "audit.sqlite3"
            timer = FakeMonotonicSleeper()
            repository = SQLiteAuditRepository(path, clock=MutableClock())
            gateway = FakeGateway([HealthSnapshot(observed_at=NOW)])
            engine = HealingEngine(
                HealingConfig(
                    mode=Mode.OBSERVE,
                    audit_path=path,
                    poll_interval=timedelta(seconds=10),
                ),
                gateway,
                repository,
                clock=MutableClock(),
                sleeper=timer.sleep,
                monotonic=timer.monotonic,
            )
            original = engine.run_once
            attempts = 0

            def flaky_cycle():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("one cycle failed")
                return original()

            engine.run_once = flaky_cycle  # type: ignore[method-assign]

            engine.run_forever(lambda: attempts >= 2)

            self.assertEqual(attempts, 2)
            self.assertEqual(gateway.read_trace, ["collect"])
            self.assertEqual(timer.sleeps, [10.0])

    def test_daemon_runs_next_cycle_after_post_action_audit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "audit.sqlite3"
            timer = FakeMonotonicSleeper()
            clock = MutableClock()
            repository = PostActionFailingRepository(
                path, clock=clock, failure="after_snapshot"
            )
            gateway = FakeGateway([pressured_snapshot()])
            engine = HealingEngine(
                HealingConfig(
                    mode=Mode.ACT,
                    audit_path=path,
                    poll_interval=timedelta(seconds=10),
                ),
                gateway,
                repository,
                clock=clock,
                sleeper=timer.sleep,
                monotonic=timer.monotonic,
            )
            original = engine.run_once
            cycles = 0

            def counted_cycle():
                nonlocal cycles
                result = original()
                cycles += 1
                return result

            engine.run_once = counted_cycle  # type: ignore[method-assign]

            engine.run_forever(lambda: cycles >= 3)

            with repository._connect() as connection:
                states = [
                    str(row["state"])
                    for row in connection.execute(
                        "SELECT state FROM cycle_transition ORDER BY id"
                    )
                ]
            self.assertEqual(cycles, 3)
            self.assertEqual(len(gateway.executed), 1)
            self.assertIn("failed", states)
            self.assertEqual(gateway.read_trace.count("collect"), 3)

    def test_same_daemon_reconciles_transiently_stuck_action_before_any_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "audit.sqlite3"
            clock = MutableClock()
            repository = TransientBoundaryRepository(
                path, clock=clock, terminal_failures=2
            )
            gateway = FakeGateway([pressured_snapshot()])
            engine = HealingEngine(
                HealingConfig(mode=Mode.ACT, audit_path=path),
                gateway,
                repository,
                clock=clock,
            )

            first = engine.run_once()
            failed_action_cycle = engine.run_once()
            recovery_cycle = engine.run_once()

            target_id = repository.list_targets()[0]["id"]
            actions = repository.list_actions(target_id)
            self.assertEqual(first.action_count, 0)
            self.assertEqual(failed_action_cycle.status, "failed")
            self.assertEqual((recovery_cycle.recovered_orphan_count, recovery_cycle.action_count), (1, 0))
            self.assertEqual(len(gateway.executed), 1)
            self.assertEqual([action["state"] for action in actions], ["outcome_unknown"])
            self.assertEqual(repository.reconcile_calls, 3)
            self.assertEqual(
                self._cycle_states(repository),
                ["started", "completed", "started", "failed", "started", "completed"],
            )

    def test_transient_reconciliation_failure_fails_closed_then_retries_next_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "audit.sqlite3"
            repository = TransientBoundaryRepository(
                path, clock=MutableClock(), reconcile_failures=1
            )
            gateway = FakeGateway([HealthSnapshot(observed_at=NOW)])
            engine = HealingEngine(
                HealingConfig(mode=Mode.OBSERVE, audit_path=path),
                gateway,
                repository,
                clock=MutableClock(),
            )

            failed = engine.run_once()
            recovered = engine.run_once()

            self.assertEqual(failed.status, "failed")
            self.assertEqual(recovered.status, "completed")
            self.assertEqual(repository.reconcile_calls, 2)
            self.assertEqual(gateway.read_trace, ["collect"])
            self.assertEqual(gateway.executed, [])
            self.assertEqual(
                self._cycle_states(repository),
                ["started", "failed", "started", "completed"],
            )


if __name__ == "__main__":
    unittest.main()
