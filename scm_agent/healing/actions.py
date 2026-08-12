"""Conservative, typed plans for the fixed PostgreSQL healing actions.

This module deliberately has no execution code and never accepts caller SQL.
The PostgreSQL gateway is responsible for live privilege, freshness, and object
type checks immediately before an approved plan is executed.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Mapping

from .config import HealingConfig
from .models import (
    ActionKind,
    ActionPlan,
    BackendTarget,
    Finding,
    IndexTarget,
    Mode,
    RuleId,
    TableTarget,
    TargetKind,
)


_TERMINATE_TEMPLATE = "SELECT pg_terminate_backend(?)"
_SYSTEM_SCHEMAS = frozenset(("information_schema", "pg_catalog", "pg_toast"))


def quote_identifier(value: str) -> str:
    """Return one PostgreSQL identifier component, safely double-quoted."""
    if not isinstance(value, str):
        raise TypeError("identifier must be a string")
    if not value:
        raise ValueError("identifier must not be empty")
    if "\x00" in value:
        raise ValueError("identifier must not contain NUL")
    return f'"{value.replace("\"", "\"\"")}"'


def qualified_identifier(schema: str, name: str) -> str:
    """Quote schema and object independently; dots never change qualification."""
    return f"{quote_identifier(schema)}.{quote_identifier(name)}"


def build_vacuum_command(schema: str, table: str) -> str:
    """Build the one permitted online VACUUM shape."""
    return f"VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) {qualified_identifier(schema, table)}"


def build_reindex_command(schema: str, index: str) -> str:
    """Build the one permitted concurrent index-rebuild shape."""
    return f"REINDEX INDEX CONCURRENTLY {qualified_identifier(schema, index)}"


def _required_string(evidence: Mapping[str, object], key: str) -> str:
    value = evidence.get(key)
    if not isinstance(value, str):
        raise ValueError(f"finding evidence {key} must be a string")
    return value


def _required_integer(evidence: Mapping[str, object], key: str) -> int:
    value = evidence.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"finding evidence {key} must be a positive integer")
    return value


def _required_aware_datetime(evidence: Mapping[str, object], key: str) -> datetime:
    value = evidence.get(key)
    if not isinstance(value, datetime):
        raise ValueError(f"finding evidence {key} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"finding evidence {key} must be timezone-aware")
    return value


def _table_target(finding: Finding) -> TableTarget:
    return TableTarget(
        database_oid=_required_integer(finding.evidence, "database_oid"),
        relation_oid=_required_integer(finding.evidence, "relation_oid"),
        schema_name=_required_string(finding.evidence, "schema_name"),
        relation_name=_required_string(finding.evidence, "relation_name"),
    )


def _backend_target(finding: Finding) -> BackendTarget:
    return BackendTarget(
        database_oid=_required_integer(finding.evidence, "database_oid"),
        pid=_required_integer(finding.evidence, "pid"),
        backend_start=_required_aware_datetime(finding.evidence, "backend_start"),
        transaction_start=_required_aware_datetime(finding.evidence, "transaction_start"),
        state_change=_required_aware_datetime(finding.evidence, "state_change"),
        state=_required_string(finding.evidence, "state"),
        role_name=_required_string(finding.evidence, "role_name"),
        application_name=_required_string(finding.evidence, "application_name"),
    )


def _manual_fingerprint(schema: str, index: str) -> str:
    identity = f"{schema}\x00{index}".encode("utf-8")
    return f"manual-reindex:{sha256(identity).hexdigest()}"


def _is_system_schema(schema: str) -> bool:
    normalized = schema.casefold()
    return normalized in _SYSTEM_SCHEMAS or normalized.startswith("pg_")


class ActionPlanner:
    """Map only safe, action-eligible findings to typed fixed-command plans."""

    def plan(self, finding: Finding, config: HealingConfig) -> ActionPlan | None:
        """Return an executable plan only for explicitly allowed automatic actions."""
        if config.mode is Mode.OBSERVE or not finding.action_eligible:
            return None

        if (
            finding.rule_id is RuleId.DEAD_TUPLE_PRESSURE
            and finding.target_kind is TargetKind.TABLE
        ):
            try:
                target = _table_target(finding)
                command = build_vacuum_command(target.schema_name, target.relation_name)
            except (TypeError, ValueError):
                return None
            return ActionPlan(
                action_kind=ActionKind.VACUUM,
                finding_fingerprint=finding.fingerprint,
                target_kind=TargetKind.TABLE,
                target=target,
                target_key=finding.target_key,
                command=command,
                parameters=target.parameters,
                mode=config.mode,
                reason="auto_vacuum_dead_tuple_pressure",
            )

        if (
            finding.rule_id is RuleId.IDLE_IN_TRANSACTION
            and finding.target_kind is TargetKind.BACKEND
            and config.mode is Mode.ACT
            and config.allow_terminate_idle_transaction
        ):
            try:
                target = _backend_target(finding)
            except ValueError:
                return None
            if target.state != "idle in transaction":
                return None
            return ActionPlan(
                action_kind=ActionKind.TERMINATE_BACKEND,
                finding_fingerprint=finding.fingerprint,
                target_kind=TargetKind.BACKEND,
                target=target,
                target_key=finding.target_key,
                command=_TERMINATE_TEMPLATE,
                parameters=target.parameters,
                mode=Mode.ACT,
                reason="auto_terminate_idle_in_transaction",
            )

        return None

    def plan_manual_reindex(
        self,
        schema: str,
        index: str,
        config: HealingConfig,
        *,
        confirmed: bool = False,
    ) -> ActionPlan:
        """Create an explicitly-approved manual concurrent reindex plan.

        A dry run is already non-executing. A real action additionally needs a
        caller's confirmation; execution-time object and privilege checks stay
        with the PostgreSQL gateway.
        """
        if not config.allow_manual_reindex:
            raise ValueError("allow_manual_reindex must be enabled")
        if config.mode is Mode.OBSERVE:
            raise ValueError("manual reindex is unavailable in observe mode")
        if config.mode is Mode.ACT and not confirmed:
            raise ValueError("manual reindex in act mode requires confirmation")
        quote_identifier(schema)
        quote_identifier(index)
        if _is_system_schema(schema):
            raise ValueError("manual reindex rejects system schema targets")

        target = IndexTarget(schema_name=schema, index_name=index)
        command = build_reindex_command(target.schema_name, target.index_name)
        fingerprint = _manual_fingerprint(schema, index)
        return ActionPlan(
            action_kind=ActionKind.REINDEX_INDEX,
            finding_fingerprint=fingerprint,
            target_kind=TargetKind.INDEX,
            target=target,
            target_key=fingerprint,
            command=command,
            parameters=target.parameters,
            mode=config.mode,
            reason="manual_reindex_requested",
        )
