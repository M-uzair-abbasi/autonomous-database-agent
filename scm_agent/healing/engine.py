"""Mode-aware, audit-first orchestration for PostgreSQL healing cycles."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import time
from typing import Callable, Mapping, Protocol

from .actions import ActionPlanner
from .audit import ActionIntentAcquisition
from .config import HealingConfig
from .models import (
    ActionKind,
    ActionPlan,
    ActionResult,
    ActionStatus,
    Finding,
    HealthSnapshot,
    Mode,
    RuleId,
)
from .postgres import CapabilityDecision
from .rules import detect_all


class HealthGateway(Protocol):
    def collect_health(self) -> HealthSnapshot: ...

    def check_plan(self, plan: ActionPlan) -> CapabilityDecision: ...

    def capture_before(self, plan: ActionPlan) -> Mapping[str, object]: ...

    def execute(self, plan: ActionPlan) -> ActionResult: ...

    def capture_after(self, plan: ActionPlan) -> Mapping[str, object]: ...


class AuditRepository(Protocol):
    def register_target(self, dsn: str, label: str) -> int: ...

    def reconcile_orphans(self) -> list[int]: ...

    def start_cycle(self, target_id: int, mode: Mode) -> int: ...

    def record_finding(self, cycle_id: int, item: Finding, collector: str) -> int: ...

    def consecutive_detection_count(self, event_id: int) -> int: ...

    def acquire_action_intent(
        self,
        event_id: int,
        item: ActionPlan,
        idempotency_key: str,
        cooldown: timedelta | None,
    ) -> ActionIntentAcquisition: ...

    def record_snapshot(
        self, action_attempt_id: int, stage: str, data: Mapping[str, object]
    ) -> int: ...

    def record_action_result(self, action_attempt_id: int, result: ActionResult) -> int: ...

    def finish_cycle(self, cycle_id: int, collector_statuses: Mapping[str, bool]) -> int: ...

    def record_cycle_transition(
        self, cycle_id: int, state: str, detail: Mapping[str, object] | None = None
    ) -> int: ...


class Clock(Protocol):
    def __call__(self) -> datetime: ...


class Sleeper(Protocol):
    def __call__(self, seconds: float) -> object: ...


@dataclass(frozen=True)
class CycleSummary:
    """Secret-free aggregate outcome; deliberately excludes targets and errors."""

    status: str
    finding_count: int = 0
    collector_error_count: int = 0
    plan_count: int = 0
    action_count: int = 0
    simulated_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    unknown_count: int = 0
    skipped_count: int = 0
    recovered_orphan_count: int = 0


@dataclass(frozen=True)
class AuditedActionSummary:
    """Secret-free terminal state for one already-correlated manual plan."""

    status: str
    action_status: str | None = None
    action_id: int | None = None
    executed: bool = False
    cycle_status: str = "completed"


_COLLECTORS = ("activity", "tables", "indexes", "saturation")
_RULE_COLLECTOR = {
    RuleId.DEAD_TUPLE_PRESSURE: "tables",
    RuleId.IDLE_IN_TRANSACTION: "activity",
    RuleId.LONG_RUNNING_QUERY: "activity",
    RuleId.CONNECTION_SATURATION: "saturation",
    RuleId.UNUSED_LARGE_INDEX: "indexes",
}
_ACTION_PRIORITY = {
    ActionKind.VACUUM: 0,
    ActionKind.TERMINATE_BACKEND: 1,
    ActionKind.REINDEX_INDEX: 2,
}
_COLLECTOR_ALIASES = {
    "activity": "activity",
    "activities": "activity",
    "table": "tables",
    "tables": "tables",
    "index": "indexes",
    "indexes": "indexes",
    "connection": "saturation",
    "connections": "saturation",
    "saturation": "saturation",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _terminalize_plan_cycle(
    audit: AuditRepository,
    cycle_id: int,
    *,
    failed: bool,
    reason: str,
) -> str:
    if not failed:
        try:
            # A manual action did not run a collector, so it must not claim a
            # collector success that could clear an unrelated anomaly event.
            audit.finish_cycle(cycle_id, {})
            return "completed"
        except BaseException as error:
            cycle_status = _fail_plan_cycle(
                audit, cycle_id, "cycle_completion_failed"
            )
            if not isinstance(error, Exception):
                raise
            return cycle_status
    return _fail_plan_cycle(audit, cycle_id, reason)


def _fail_plan_cycle(audit: AuditRepository, cycle_id: int, reason: str) -> str:
    """Best-effort terminalization which never masks an active interrupt."""

    try:
        audit.record_cycle_transition(cycle_id, "failed", {"reason": reason})
        return "failed"
    except Exception:
        return "unterminated"


def _manual_failure_result(plan: ActionPlan, clock: Clock, reason: str) -> ActionResult:
    occurred_at = clock()
    return ActionResult(
        plan.action_kind,
        ActionStatus.FAILED,
        plan.target_key,
        occurred_at,
        occurred_at,
        error=reason,
    )


def _persist_terminal_with_fallback(
    audit: AuditRepository,
    action_id: int,
    plan: ActionPlan,
    result: ActionResult,
    clock: Clock,
) -> tuple[str | None, bool]:
    try:
        audit.record_action_result(action_id, result)
        return result.status.value, False
    except Exception:
        occurred_at = clock()
        fallback = ActionResult(
            plan.action_kind,
            ActionStatus.OUTCOME_UNKNOWN,
            plan.target_key,
            occurred_at,
            occurred_at,
            error="terminal_result_persistence_failed",
        )
        try:
            audit.record_action_result(action_id, fallback)
            return ActionStatus.OUTCOME_UNKNOWN.value, True
        except Exception:
            return None, True


def run_audited_plan(
    cycle_id: int,
    event_id: int,
    plan: ActionPlan,
    gateway: HealthGateway,
    audit: AuditRepository,
    *,
    clock: Clock = _utc_now,
) -> AuditedActionSummary:
    """Run one manual plan with a durable intent and failure-safe terminalization.

    The caller owns target registration, cycle start, correlation, and orphan
    reconciliation. This helper owns every live/action stage after correlation.
    """
    try:
        decision = gateway.check_plan(plan)
    except BaseException as error:
        cycle_status = _terminalize_plan_cycle(
            audit, cycle_id, failed=True, reason="capability_check_failed"
        )
        if not isinstance(error, Exception):
            raise
        return AuditedActionSummary("failed", cycle_status=cycle_status)
    if not decision.allowed:
        cycle_status = _terminalize_plan_cycle(
            audit, cycle_id, failed=False, reason="precondition_rejected"
        )
        return AuditedActionSummary(
            "precondition_rejected", cycle_status=cycle_status
        )

    before_data: Mapping[str, object] | None = None
    if plan.mode is Mode.DRY_RUN:
        try:
            before_data = gateway.capture_before(plan)
        except BaseException as error:
            cycle_status = _terminalize_plan_cycle(
                audit, cycle_id, failed=True, reason="before_snapshot_failed"
            )
            if not isinstance(error, Exception):
                raise
            return AuditedActionSummary("failed", cycle_status=cycle_status)

    try:
        acquisition = audit.acquire_action_intent(
            event_id,
            plan,
            f"manual-{sha256(f'{cycle_id}:{plan.finding_fingerprint}:{plan.mode.value}'.encode('utf-8')).hexdigest()}",
            None,
        )
    except BaseException as error:
        cycle_status = _terminalize_plan_cycle(
            audit, cycle_id, failed=True, reason="intent_persistence_failed"
        )
        if not isinstance(error, Exception):
            raise
        return AuditedActionSummary("failed", cycle_status=cycle_status)
    if acquisition.status != "acquired" or acquisition.action_id is None:
        cycle_status = _terminalize_plan_cycle(
            audit, cycle_id, failed=False, reason="intent_not_acquired"
        )
        return AuditedActionSummary(acquisition.status, cycle_status=cycle_status)

    action_id = acquisition.action_id
    if plan.mode is Mode.DRY_RUN:
        failed = False
        try:
            assert before_data is not None
            audit.record_snapshot(action_id, "before", before_data)
        except BaseException as error:
            if not isinstance(error, Exception):
                _fail_plan_cycle(audit, cycle_id, "dry_run_snapshot_interrupted")
                raise
            failed = True
        try:
            after_data = gateway.capture_after(plan)
            audit.record_snapshot(action_id, "after", after_data)
        except BaseException as error:
            if not isinstance(error, Exception):
                _fail_plan_cycle(audit, cycle_id, "dry_run_snapshot_interrupted")
                raise
            failed = True
        cycle_status = _terminalize_plan_cycle(
            audit,
            cycle_id,
            failed=failed,
            reason="dry_run_snapshot_failed",
        )
        return AuditedActionSummary(
            "failed" if failed or cycle_status != "completed" else "simulated",
            action_status=ActionStatus.SIMULATED.value,
            action_id=action_id,
            cycle_status=cycle_status,
        )

    try:
        before_data = gateway.capture_before(plan)
        audit.record_snapshot(action_id, "before", before_data)
    except BaseException as error:
        if not isinstance(error, Exception):
            _fail_plan_cycle(audit, cycle_id, "before_snapshot_interrupted")
            raise
        try:
            action_status, _ = _persist_terminal_with_fallback(
                audit,
                action_id,
                plan,
                _manual_failure_result(plan, clock, "before_snapshot_failed"),
                clock,
            )
        except BaseException:
            _fail_plan_cycle(audit, cycle_id, "failure_persistence_interrupted")
            raise
        cycle_status = _terminalize_plan_cycle(
            audit,
            cycle_id,
            failed=True,
            reason="before_snapshot_failed",
        )
        return AuditedActionSummary(
            "failed",
            action_status=action_status,
            action_id=action_id,
            cycle_status=cycle_status,
        )

    try:
        audit.record_action_result(
            action_id,
            ActionResult(plan.action_kind, ActionStatus.EXECUTING, plan.target_key, clock()),
        )
    except BaseException as error:
        cycle_status = _terminalize_plan_cycle(
            audit, cycle_id, failed=True, reason="executing_persistence_failed"
        )
        if not isinstance(error, Exception):
            raise
        return AuditedActionSummary(
            "failed",
            action_status=ActionStatus.PLANNED.value,
            action_id=action_id,
            cycle_status=cycle_status,
        )

    try:
        result = gateway.execute(plan)
    except BaseException as error:
        if not isinstance(error, Exception):
            _fail_plan_cycle(audit, cycle_id, "gateway_dispatch_interrupted")
            raise
        try:
            occurred_at = clock()
            result = ActionResult(
                plan.action_kind,
                ActionStatus.OUTCOME_UNKNOWN,
                plan.target_key,
                occurred_at,
                occurred_at,
                error="gateway_dispatch_exception",
            )
        except BaseException:
            _fail_plan_cycle(audit, cycle_id, "dispatch_result_interrupted")
            raise

    post_failed = False
    try:
        after_data = gateway.capture_after(plan)
        audit.record_snapshot(action_id, "after", after_data)
    except BaseException as error:
        if not isinstance(error, Exception):
            try:
                _persist_terminal_with_fallback(audit, action_id, plan, result, clock)
            except BaseException as persistence_error:
                _fail_plan_cycle(
                    audit, cycle_id, "terminal_result_persistence_interrupted"
                )
                if not isinstance(persistence_error, Exception):
                    raise
            _fail_plan_cycle(audit, cycle_id, "after_snapshot_interrupted")
            raise
        post_failed = True

    try:
        action_status, persistence_failed = _persist_terminal_with_fallback(
            audit, action_id, plan, result, clock
        )
    except BaseException:
        _fail_plan_cycle(audit, cycle_id, "terminal_result_persistence_interrupted")
        raise
    post_failed = post_failed or persistence_failed
    cycle_status = _terminalize_plan_cycle(
        audit,
        cycle_id,
        failed=post_failed,
        reason="post_action_audit_failed",
    )
    operation_status = (
        "failed"
        if post_failed or cycle_status != "completed"
        else (action_status or "failed")
    )
    return AuditedActionSummary(
        operation_status,
        action_status=action_status,
        action_id=action_id,
        executed=True,
        cycle_status=cycle_status,
    )


class HealingEngine:
    """Run serial collect/detect/plan/action sagas for one configured target."""

    def __init__(
        self,
        config: HealingConfig,
        gateway: HealthGateway,
        audit: AuditRepository,
        *,
        clock: Clock = _utc_now,
        planner: ActionPlanner | None = None,
        sleeper: Sleeper | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._audit = audit
        self._clock = clock
        self._planner = planner or ActionPlanner()
        self._sleeper = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._target_id = audit.register_target(config.database_dsn, "scm-ovs-target")

    def run_once(self) -> CycleSummary:
        cycle_id = self._audit.start_cycle(self._target_id, self._config.mode)
        try:
            recovered = len(self._audit.reconcile_orphans())
        except Exception:
            self._try_fail_cycle(cycle_id, "orphan_reconciliation_failed")
            return CycleSummary(status="failed")
        try:
            return self._run_started_cycle(cycle_id, recovered)
        except Exception:
            self._try_fail_cycle(cycle_id, "cycle_exception")
            raise

    def _run_started_cycle(self, cycle_id: int, recovered: int) -> CycleSummary:
        try:
            snapshot = self._gateway.collect_health()
        except Exception:
            self._audit.finish_cycle(cycle_id, {collector: False for collector in _COLLECTORS})
            return CycleSummary(
                status="failed",
                collector_error_count=len(_COLLECTORS),
                recovered_orphan_count=recovered,
            )
        failed_collectors = {
            _COLLECTOR_ALIASES.get(error.collector, error.collector)
            for error in snapshot.errors
        }
        collector_statuses = {
            collector: collector not in failed_collectors for collector in _COLLECTORS
        }
        detection_snapshot = snapshot
        if not collector_statuses["saturation"]:
            detection_snapshot = replace(snapshot, connection=None)
        findings = detect_all(detection_snapshot, self._config.policy)

        correlated: list[tuple[Finding, int]] = []
        for finding in findings:
            event_id = self._audit.record_finding(
                cycle_id, finding, _RULE_COLLECTOR[finding.rule_id]
            )
            correlated.append((finding, event_id))

        planned: list[tuple[ActionPlan, int]] = []
        for finding, event_id in correlated:
            plan = self._planner.plan(finding, self._config)
            if plan is not None:
                planned.append((plan, event_id))
        planned.sort(
            key=lambda item: (
                _ACTION_PRIORITY[item[0].action_kind],
                item[0].target_key,
                item[0].finding_fingerprint,
            )
        )

        action_count = simulated = succeeded = failed = unknown = skipped = 0
        cycle_status = "completed"
        for plan, event_id in planned:
            if recovered > 0:
                skipped += len(planned)
                break
            if action_count >= 1:
                break
            required_detections = max(2, self._config.required_consecutive_detections)
            if self._audit.consecutive_detection_count(event_id) < required_detections:
                skipped += 1
                continue
            decision = self._gateway.check_plan(plan)
            if not decision.allowed:
                skipped += 1
                continue

            try:
                acquisition = self._audit.acquire_action_intent(
                    event_id,
                    plan,
                    self._idempotency_key(cycle_id, plan),
                    self._cooldown(plan),
                )
            except Exception:
                cycle_status = "failed"
                failed += 1
                break
            if acquisition.status != "acquired" or acquisition.action_id is None:
                skipped += 1
                continue

            action_count += 1
            action_id = acquisition.action_id
            try:
                before = self._gateway.capture_before(plan)
                self._audit.record_snapshot(action_id, "before", before)
            except Exception:
                failed += 1
                occurred_at = self._clock()
                self._audit.record_action_result(
                    action_id,
                    ActionResult(
                        plan.action_kind,
                        ActionStatus.FAILED,
                        plan.target_key,
                        occurred_at,
                        occurred_at,
                        error="before_snapshot_failed",
                    ),
                )
                continue

            if plan.mode is Mode.DRY_RUN:
                try:
                    after = self._gateway.capture_after(plan)
                    self._audit.record_snapshot(action_id, "after", after)
                except Exception:
                    cycle_status = "failed"
                    failed += 1
                else:
                    simulated += 1
                continue

            self._audit.record_action_result(
                action_id,
                ActionResult(plan.action_kind, ActionStatus.EXECUTING, plan.target_key, self._clock()),
            )
            try:
                result = self._gateway.execute(plan)
            except Exception:
                occurred_at = self._clock()
                result = ActionResult(
                    plan.action_kind,
                    ActionStatus.OUTCOME_UNKNOWN,
                    plan.target_key,
                    occurred_at,
                    occurred_at,
                    error="gateway_dispatch_exception",
                )
            post_failed = False
            try:
                after = self._gateway.capture_after(plan)
            except Exception:
                post_failed = True
            else:
                try:
                    self._audit.record_snapshot(action_id, "after", after)
                except Exception:
                    post_failed = True

            result_persisted = False
            fallback_persisted = False
            try:
                self._audit.record_action_result(action_id, result)
                result_persisted = True
            except Exception:
                post_failed = True
                occurred_at = self._clock()
                fallback = ActionResult(
                    plan.action_kind,
                    ActionStatus.OUTCOME_UNKNOWN,
                    plan.target_key,
                    occurred_at,
                    occurred_at,
                    error="terminal_result_persistence_failed",
                )
                try:
                    self._audit.record_action_result(action_id, fallback)
                    result_persisted = True
                    fallback_persisted = True
                except Exception:
                    pass

            if post_failed:
                cycle_status = "failed"
                if fallback_persisted or not result_persisted:
                    unknown += 1
                elif result.status is ActionStatus.SUCCEEDED:
                    # The database result is retained, but the incomplete saga
                    # must not be reported as a completed success.
                    failed += 1
                elif result.status is ActionStatus.PRECONDITION_REJECTED:
                    skipped += 1
                    failed += 1
                elif result.status is ActionStatus.OUTCOME_UNKNOWN:
                    unknown += 1
                else:
                    failed += 1
                continue

            if result.status is ActionStatus.SUCCEEDED:
                succeeded += 1
            elif result.status is ActionStatus.OUTCOME_UNKNOWN:
                unknown += 1
            elif result.status is ActionStatus.PRECONDITION_REJECTED:
                skipped += 1
            else:
                failed += 1

        if cycle_status == "completed":
            self._audit.finish_cycle(cycle_id, collector_statuses)
        else:
            self._try_fail_cycle(cycle_id, "cycle_stage_failure")
        return CycleSummary(
            status=cycle_status,
            finding_count=len(findings),
            collector_error_count=len(snapshot.errors),
            plan_count=len(planned),
            action_count=action_count,
            simulated_count=simulated,
            succeeded_count=succeeded,
            failed_count=failed,
            unknown_count=unknown,
            skipped_count=skipped,
            recovered_orphan_count=recovered,
        )

    def _try_fail_cycle(self, cycle_id: int, reason: str) -> None:
        try:
            self._audit.record_cycle_transition(cycle_id, "failed", {"reason": reason})
        except Exception:
            pass

    def _cooldown(self, plan: ActionPlan) -> timedelta | None:
        if plan.mode is Mode.ACT and plan.action_kind is ActionKind.VACUUM:
            return self._config.vacuum_cooldown
        return None

    @staticmethod
    def _idempotency_key(cycle_id: int, plan: ActionPlan) -> str:
        if plan.mode is Mode.ACT and plan.action_kind is ActionKind.TERMINATE_BACKEND:
            value = f"identity:{plan.finding_fingerprint}"
        else:
            value = f"cycle:{cycle_id}:{plan.mode.value}:{plan.action_kind.value}:{plan.finding_fingerprint}"
        return f"engine-{sha256(value.encode('utf-8')).hexdigest()}"

    def run_forever(self, stop: Callable[[], bool]) -> None:
        interval = self._config.poll_interval.total_seconds()
        while not stop():
            started = self._monotonic()
            try:
                self.run_once()
            except Exception:
                pass
            if stop():
                return
            remaining = interval - (self._monotonic() - started)
            if remaining > 0:
                self._sleeper(remaining)
