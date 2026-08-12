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
        self._recovered = False

    def run_once(self) -> CycleSummary:
        recovered = 0
        if not self._recovered:
            recovered = len(self._audit.reconcile_orphans())
            self._recovered = True

        cycle_id = self._audit.start_cycle(self._target_id, self._config.mode)
        try:
            snapshot = self._gateway.collect_health()
        except Exception:
            self._audit.finish_cycle(cycle_id, {collector: False for collector in _COLLECTORS})
            return CycleSummary(status="failed", collector_error_count=len(_COLLECTORS), recovered_orphan_count=recovered)
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
            if action_count >= self._config.maximum_actions_per_cycle:
                break
            if self._audit.consecutive_detection_count(event_id) < self._config.required_consecutive_detections:
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
                after = self._gateway.capture_after(plan)
                self._audit.record_snapshot(action_id, "after", after)
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
            after = self._gateway.capture_after(plan)
            self._audit.record_snapshot(action_id, "after", after)
            stored_result = result
            if result.status is ActionStatus.PRECONDITION_REJECTED:
                stored_result = ActionResult(
                    result.action_kind,
                    ActionStatus.FAILED,
                    result.target_key,
                    result.started_at,
                    result.completed_at,
                    error=result.error,
                    evidence={**dict(result.evidence), "gateway_status": result.status.value},
                )
            self._audit.record_action_result(action_id, stored_result)
            if result.status is ActionStatus.SUCCEEDED:
                succeeded += 1
            elif result.status is ActionStatus.OUTCOME_UNKNOWN:
                unknown += 1
            elif result.status is ActionStatus.PRECONDITION_REJECTED:
                skipped += 1
            else:
                failed += 1

        self._audit.finish_cycle(cycle_id, collector_statuses)
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
            self.run_once()
            if stop():
                return
            remaining = interval - (self._monotonic() - started)
            if remaining > 0:
                self._sleeper(remaining)
