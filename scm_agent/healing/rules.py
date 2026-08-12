"""Pure, conservative anomaly rules over immutable observation samples."""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Iterable

from .config import RulePolicy
from .models import (
    ActivitySample,
    ConnectionSample,
    Finding,
    HealthSnapshot,
    IndexStatsSample,
    RuleId,
    Severity,
    TableStatsSample,
    TargetKind,
)


def _age(observed_at: datetime, started_at: datetime | None) -> timedelta | None:
    """Return a non-negative observation-relative age, or ``None`` if invalid."""
    if started_at is None or started_at > observed_at:
        return None
    return observed_at - started_at


def _fingerprint(*parts: object) -> str:
    stable_value = "|".join(str(part) for part in parts)
    return sha256(stable_value.encode("utf-8")).hexdigest()


def _finding(
    *,
    rule_id: RuleId,
    severity: Severity,
    target_kind: TargetKind,
    target_key: str,
    evidence: dict[str, object],
    recommendation: str,
    action_eligible: bool,
    observed_at: datetime,
    fingerprint_parts: tuple[object, ...],
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        target_kind=target_kind,
        target_key=target_key,
        evidence=evidence,
        recommendation=recommendation,
        action_eligible=action_eligible,
        observed_at=observed_at,
        fingerprint=_fingerprint(rule_id.value, *fingerprint_parts),
    )


def detect_table_pressure(
    samples: Iterable[TableStatsSample], policy: RulePolicy
) -> tuple[Finding, ...]:
    """Find relations whose estimate-based dead-tuple pressure crosses every gate."""
    findings: list[Finding] = []
    for sample in samples:
        if sample.n_live_tup < 0 or sample.n_dead_tup < 0:
            continue
        total_rows = sample.n_live_tup + sample.n_dead_tup
        if total_rows <= 0:
            continue
        dead_tuple_ratio = sample.n_dead_tup / total_rows
        if (
            total_rows <= policy.minimum_table_rows
            or sample.n_dead_tup <= policy.minimum_dead_tuples
            or dead_tuple_ratio <= policy.dead_tuple_ratio
        ):
            continue
        target_key = f"{sample.database_oid}:{sample.relation_oid}"
        findings.append(
            _finding(
                rule_id=RuleId.DEAD_TUPLE_PRESSURE,
                severity=Severity.WARNING,
                target_kind=TargetKind.TABLE,
                target_key=target_key,
                evidence={
                    "database_oid": sample.database_oid,
                    "relation_oid": sample.relation_oid,
                    "schema_name": sample.schema_name,
                    "relation_name": sample.relation_name,
                    "live_tuples": sample.n_live_tup,
                    "dead_tuples": sample.n_dead_tup,
                    "total_tuples": total_rows,
                    "dead_tuple_ratio": round(dead_tuple_ratio, 6),
                },
                recommendation="Run an online VACUUM after the engine's safety gates pass.",
                action_eligible=True,
                observed_at=sample.observed_at,
                fingerprint_parts=(sample.database_oid, sample.relation_oid),
            )
        )
    return tuple(sorted(findings, key=lambda finding: finding.target_key))


def _is_eligible_client_session(sample: ActivitySample) -> bool:
    return not sample.is_self and sample.backend_type == "client backend"


def detect_idle_transactions(
    samples: Iterable[ActivitySample], policy: RulePolicy
) -> tuple[Finding, ...]:
    """Find lock-holding idle transactions without treating ordinary idle as harmful."""
    findings: list[Finding] = []
    monitored_states = {"idle in transaction", "idle in transaction (aborted)"}
    for sample in samples:
        if not _is_eligible_client_session(sample) or sample.state not in monitored_states:
            continue
        age = _age(sample.observed_at, sample.state_change)
        if age is None or age <= policy.idle_transaction_warning:
            continue
        action_eligible = (
            sample.state == "idle in transaction" and age > policy.idle_transaction_action
        )
        target_key = f"{sample.database_oid}:{sample.pid}"
        findings.append(
            _finding(
                rule_id=RuleId.IDLE_IN_TRANSACTION,
                severity=Severity.CRITICAL if action_eligible else Severity.WARNING,
                target_kind=TargetKind.BACKEND,
                target_key=target_key,
                evidence={
                    "database_oid": sample.database_oid,
                    "pid": sample.pid,
                    "role_name": sample.role_name,
                    "application_name": sample.application_name,
                    "state": sample.state,
                    "state_age_seconds": int(age.total_seconds()),
                },
                recommendation="Ask the client to finish its transaction; termination remains separately gated.",
                action_eligible=action_eligible,
                observed_at=sample.observed_at,
                fingerprint_parts=(
                    sample.database_oid,
                    sample.pid,
                    sample.backend_start.isoformat(),
                    sample.transaction_start.isoformat() if sample.transaction_start else "",
                    sample.state_change.isoformat() if sample.state_change else "",
                ),
            )
        )
    return tuple(sorted(findings, key=lambda finding: finding.target_key))


def detect_long_queries(
    samples: Iterable[ActivitySample], policy: RulePolicy
) -> tuple[Finding, ...]:
    """Report only active, observation-old queries; raw query text is never retained."""
    findings: list[Finding] = []
    for sample in samples:
        if not _is_eligible_client_session(sample) or sample.state != "active":
            continue
        age = _age(sample.observed_at, sample.query_start)
        if age is None or age <= policy.long_query:
            continue
        target_key = f"{sample.database_oid}:{sample.pid}"
        findings.append(
            _finding(
                rule_id=RuleId.LONG_RUNNING_QUERY,
                severity=Severity.WARNING,
                target_kind=TargetKind.BACKEND,
                target_key=target_key,
                evidence={
                    "database_oid": sample.database_oid,
                    "pid": sample.pid,
                    "role_name": sample.role_name,
                    "application_name": sample.application_name,
                    "query_age_seconds": int(age.total_seconds()),
                    "wait_event_type": sample.wait_event_type,
                    "wait_event": sample.wait_event,
                },
                recommendation="Inspect the active query and blocking chain before intervening.",
                action_eligible=False,
                observed_at=sample.observed_at,
                fingerprint_parts=(
                    sample.database_oid,
                    sample.pid,
                    sample.backend_start.isoformat(),
                    sample.query_start.isoformat() if sample.query_start else "",
                ),
            )
        )
    return tuple(sorted(findings, key=lambda finding: finding.target_key))


def detect_connection_saturation(
    sample: ConnectionSample | None, policy: RulePolicy
) -> tuple[Finding, ...]:
    """Classify cluster-wide client-backend pressure without choosing a victim."""
    if (
        sample is None
        or sample.maximum_connections <= 0
        or sample.client_backend_count < 0
        or sample.reserved_connections < 0
    ):
        return ()
    ratio = sample.client_backend_count / sample.maximum_connections
    if ratio <= policy.connection_warning_ratio:
        return ()
    severity = (
        Severity.CRITICAL
        if ratio > policy.connection_critical_ratio
        else Severity.WARNING
    )
    return (
        _finding(
            rule_id=RuleId.CONNECTION_SATURATION,
            severity=severity,
            target_kind=TargetKind.CLUSTER,
            target_key="cluster",
            evidence={
                "client_backend_count": sample.client_backend_count,
                "maximum_connections": sample.maximum_connections,
                "reserved_connections": sample.reserved_connections,
                "connection_ratio": round(ratio, 6),
            },
            recommendation="Reduce client connection demand or increase capacity; no automatic eviction is used.",
            action_eligible=False,
            observed_at=sample.observed_at,
            fingerprint_parts=("cluster",),
        ),
    )


def _statistics_window_start(sample: IndexStatsSample) -> datetime | None:
    starts = (sample.stats_reset, sample.server_started)
    if not any(starts):
        return None
    if any(start is not None and start > sample.observed_at for start in starts):
        return None
    return max(start for start in starts if start is not None)


def detect_unused_indexes(
    samples: Iterable[IndexStatsSample], policy: RulePolicy
) -> tuple[Finding, ...]:
    """Report possibly unused, large non-constraint indexes as advisory findings."""
    findings: list[Finding] = []
    for sample in samples:
        if (
            sample.index_size_bytes < 0
            or sample.idx_scan < 0
            or not sample.is_valid
            or not sample.is_ready
            or not sample.is_live
            or sample.is_primary
            or sample.is_unique
            or sample.is_constraint
            or sample.index_size_bytes <= policy.unused_index_minimum_bytes
            or sample.idx_scan > policy.unused_index_maximum_scans
        ):
            continue
        window_start = _statistics_window_start(sample)
        stats_age = _age(sample.observed_at, window_start)
        if stats_age is None or stats_age <= policy.minimum_statistics_age:
            continue
        target_key = f"{sample.database_oid}:{sample.index_oid}"
        findings.append(
            _finding(
                rule_id=RuleId.UNUSED_LARGE_INDEX,
                severity=Severity.WARNING,
                target_kind=TargetKind.INDEX,
                target_key=target_key,
                evidence={
                    "database_oid": sample.database_oid,
                    "table_oid": sample.table_oid,
                    "index_oid": sample.index_oid,
                    "schema_name": sample.schema_name,
                    "table_name": sample.table_name,
                    "index_name": sample.index_name,
                    "index_size_bytes": sample.index_size_bytes,
                    "idx_scan": sample.idx_scan,
                    "statistics_age_seconds": int(stats_age.total_seconds()),
                },
                recommendation="Review index usage before any manual maintenance or removal decision.",
                action_eligible=False,
                observed_at=sample.observed_at,
                fingerprint_parts=(sample.database_oid, sample.index_oid),
            )
        )
    return tuple(sorted(findings, key=lambda finding: finding.target_key))


def detect_all(snapshot: HealthSnapshot, policy: RulePolicy) -> tuple[Finding, ...]:
    """Evaluate every available observation group in a stable order."""
    failed_collectors = {error.collector for error in snapshot.errors}
    findings: list[Finding] = []
    if not failed_collectors.intersection(("table", "tables")):
        findings.extend(detect_table_pressure(snapshot.tables, policy))
    if not failed_collectors.intersection(("activity", "activities")):
        findings.extend(detect_idle_transactions(snapshot.activities, policy))
        findings.extend(detect_long_queries(snapshot.activities, policy))
    if not failed_collectors.intersection(("connection", "connections")):
        findings.extend(detect_connection_saturation(snapshot.connection, policy))
    if not failed_collectors.intersection(("index", "indexes")):
        findings.extend(detect_unused_indexes(snapshot.indexes, policy))
    return tuple(sorted(findings, key=lambda finding: (finding.rule_id.value, finding.target_key)))
