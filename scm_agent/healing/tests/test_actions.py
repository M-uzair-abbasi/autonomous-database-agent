"""Action-planning contracts: fixed PostgreSQL commands, never caller SQL."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import unittest

from scm_agent.healing.actions import (
    ActionPlanner,
    build_reindex_command,
    build_vacuum_command,
    qualified_identifier,
    quote_identifier,
)
from scm_agent.healing.config import HealingConfig
from scm_agent.healing.models import (
    ActionKind,
    ActionPlan,
    BackendTarget,
    Finding,
    IndexTarget,
    Mode,
    RuleId,
    Severity,
    TableTarget,
    TargetKind,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
BACKEND_START = NOW - timedelta(hours=1)
TRANSACTION_START = NOW - timedelta(minutes=30)
STATE_CHANGE = NOW - timedelta(minutes=20)


def finding(
    rule_id: RuleId,
    target_kind: TargetKind,
    *,
    action_eligible: bool = True,
    evidence: dict[str, object] | None = None,
) -> Finding:
    default_evidence: dict[str, object] = {
        "database_oid": 17,
        "relation_oid": 42,
        "schema_name": "public",
        "relation_name": "orders",
        "pid": 123,
        "backend_start": BACKEND_START,
        "transaction_start": TRANSACTION_START,
        "state_change": STATE_CHANGE,
        "state": "idle in transaction",
        "role_name": "scm",
        "application_name": "psql",
    }
    if evidence:
        default_evidence.update(evidence)
    return Finding(
        rule_id=rule_id,
        severity=Severity.CRITICAL,
        target_kind=target_kind,
        target_key="17:42",
        evidence=default_evidence,
        recommendation="test fixture",
        action_eligible=action_eligible,
        observed_at=NOW,
        fingerprint=f"{rule_id.value}:fixture",
    )


class IdentifierQuotingTests(unittest.TestCase):
    def test_quotes_each_identifier_component_from_literal_adversarial_corpus(self) -> None:
        cases = (
            ("select", '"select"'),
            ("two words", '"two words"'),
            ("mixedCase", '"mixedCase"'),
            ('Odd"Schema', '"Odd""Schema"'),
            ("données", '"données"'),
            ("object.with.dot", '"object.with.dot"'),
            ("semi;colon", '"semi;colon"'),
            ("comment--looking", '"comment--looking"'),
            ("comment/*looking*/", '"comment/*looking*/"'),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(quote_identifier(value), expected)
                self.assertEqual(qualified_identifier(value, value), f"{expected}.{expected}")

    def test_vacuum_uses_exact_canonical_command_and_separate_quotes(self) -> None:
        self.assertEqual(
            build_vacuum_command('Odd"Schema', "select"),
            'VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) "Odd""Schema"."select"',
        )
        self.assertEqual(
            build_vacuum_command("tenant.a", "orders; VACUUM FULL"),
            'VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) "tenant.a"."orders; VACUUM FULL"',
        )

    def test_reindex_uses_exact_canonical_concurrent_command(self) -> None:
        self.assertEqual(
            build_reindex_command("public", "orders customer idx"),
            'REINDEX INDEX CONCURRENTLY "public"."orders customer idx"',
        )

    def test_rejects_empty_or_nul_containing_identifier_components(self) -> None:
        for value in ("", "before\x00after"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    quote_identifier(value)
                with self.assertRaises(ValueError):
                    qualified_identifier(value, "orders")
                with self.assertRaises(ValueError):
                    build_vacuum_command("public", value)
                with self.assertRaises(ValueError):
                    build_reindex_command(value, "orders_idx")


class ActionPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = ActionPlanner()

    def test_public_planners_reject_raw_sql_arguments(self) -> None:
        table_finding = finding(RuleId.DEAD_TUPLE_PRESSURE, TargetKind.TABLE)
        config = HealingConfig(mode=Mode.DRY_RUN)

        with self.assertRaises(TypeError):
            self.planner.plan(table_finding, config, sql="VACUUM FULL orders")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            self.planner.plan_manual_reindex(
                "public", "orders_idx", config, confirmed=True, sql="DROP TABLE orders"  # type: ignore[call-arg]
            )

    def test_manual_index_identity_uses_an_opaque_non_sql_prefix(self) -> None:
        config = HealingConfig(mode=Mode.DRY_RUN, allow_manual_reindex=True)

        first = self.planner.plan_manual_reindex("public", "orders_idx", config)
        second = self.planner.plan_manual_reindex("public", "customers_idx", config)

        self.assertTrue(first.target_key.startswith("manual-index-target:"))
        self.assertTrue(second.target_key.startswith("manual-index-target:"))
        self.assertNotEqual(first.target_key, second.target_key)
        self.assertNotIn("reindex", first.target_key.casefold())

    def test_table_pressure_maps_to_fixed_vacuum_in_dry_run_and_act(self) -> None:
        item = finding(RuleId.DEAD_TUPLE_PRESSURE, TargetKind.TABLE)

        dry_run = self.planner.plan(item, HealingConfig(mode=Mode.DRY_RUN))
        act = self.planner.plan(item, HealingConfig(mode=Mode.ACT))

        self.assertIsNotNone(dry_run)
        self.assertIsNotNone(act)
        assert dry_run is not None and act is not None
        self.assertEqual(dry_run.action_kind.value, "vacuum")
        self.assertEqual(dry_run.target_kind, TargetKind.TABLE)
        self.assertEqual(dry_run.command, 'VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) "public"."orders"')
        self.assertEqual(
            dry_run.target,
            TableTarget(
                database_oid=17,
                relation_oid=42,
                schema_name="public",
                relation_name="orders",
            ),
        )
        self.assertEqual(
            dry_run.parameters,
            {
                "database_oid": 17,
                "relation_oid": 42,
                "schema": "public",
                "table": "orders",
            },
        )
        self.assertEqual(dry_run.mode, Mode.DRY_RUN)
        self.assertEqual(act.mode, Mode.ACT)
        self.assertEqual(dry_run.reason, "auto_vacuum_dead_tuple_pressure")

    def test_observe_never_creates_an_executable_plan(self) -> None:
        item = finding(RuleId.DEAD_TUPLE_PRESSURE, TargetKind.TABLE)

        self.assertIsNone(self.planner.plan(item, HealingConfig(mode=Mode.OBSERVE)))

    def test_idle_transaction_requires_eligibility_flag_and_act_mode(self) -> None:
        item = finding(RuleId.IDLE_IN_TRANSACTION, TargetKind.BACKEND)

        self.assertIsNone(self.planner.plan(item, HealingConfig(mode=Mode.ACT)))
        self.assertIsNone(
            self.planner.plan(
                finding(RuleId.IDLE_IN_TRANSACTION, TargetKind.BACKEND, action_eligible=False),
                HealingConfig(mode=Mode.ACT, allow_terminate_idle_transaction=True),
            )
        )
        self.assertIsNone(
            self.planner.plan(
                item, HealingConfig(mode=Mode.DRY_RUN, allow_terminate_idle_transaction=True)
            )
        )

        plan = self.planner.plan(
            item, HealingConfig(mode=Mode.ACT, allow_terminate_idle_transaction=True)
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.action_kind.value, "terminate_backend")
        self.assertEqual(plan.command, "SELECT pg_terminate_backend(?)")
        self.assertEqual(plan.parameters["pid"], 123)
        self.assertEqual(
            plan.target,
            BackendTarget(
                database_oid=17,
                pid=123,
                backend_start=BACKEND_START,
                transaction_start=TRANSACTION_START,
                state_change=STATE_CHANGE,
                state="idle in transaction",
                role_name="scm",
                application_name="psql",
            ),
        )
        self.assertEqual(plan.mode, Mode.ACT)
        self.assertEqual(plan.reason, "auto_terminate_idle_in_transaction")

    def test_termination_rejects_missing_or_naive_session_identity(self) -> None:
        enabled = HealingConfig(mode=Mode.ACT, allow_terminate_idle_transaction=True)

        self.assertIsNone(
            self.planner.plan(
                finding(
                    RuleId.IDLE_IN_TRANSACTION,
                    TargetKind.BACKEND,
                    evidence={"transaction_start": None},
                ),
                enabled,
            )
        )
        self.assertIsNone(
            self.planner.plan(
                finding(
                    RuleId.IDLE_IN_TRANSACTION,
                    TargetKind.BACKEND,
                    evidence={"backend_start": BACKEND_START.replace(tzinfo=None)},
                ),
                enabled,
            )
        )

    def test_same_table_name_with_different_oids_creates_distinct_typed_targets(self) -> None:
        config = HealingConfig(mode=Mode.DRY_RUN)
        first = self.planner.plan(
            finding(RuleId.DEAD_TUPLE_PRESSURE, TargetKind.TABLE), config
        )
        second = self.planner.plan(
            finding(
                RuleId.DEAD_TUPLE_PRESSURE,
                TargetKind.TABLE,
                evidence={"relation_oid": 43},
            ),
            config,
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertNotEqual(first.target, second.target)
        self.assertEqual(first.target.schema_name, second.target.schema_name)
        self.assertEqual(first.target.relation_name, second.target.relation_name)

    def test_target_evidence_and_parameters_are_immutable_copies(self) -> None:
        source_evidence = {
            "database_oid": 17,
            "relation_oid": 42,
            "schema_name": "public",
            "relation_name": "orders",
            "nested": {"relation_oid": 42},
        }
        item = finding(
            RuleId.DEAD_TUPLE_PRESSURE,
            TargetKind.TABLE,
            evidence=source_evidence,
        )
        source_evidence["relation_oid"] = 999
        source_evidence["nested"]["relation_oid"] = 999
        plan = self.planner.plan(item, HealingConfig(mode=Mode.DRY_RUN))

        self.assertEqual(item.evidence["relation_oid"], 42)
        self.assertEqual(item.evidence["nested"]["relation_oid"], 42)
        self.assertIsNotNone(plan)
        assert plan is not None
        with self.assertRaises(TypeError):
            plan.parameters["relation_oid"] = 999  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            plan.target.relation_oid = 999  # type: ignore[misc]

    def test_model_rejects_incomplete_backend_identity_and_mismatched_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "transaction_start"):
            BackendTarget(
                database_oid=17,
                pid=123,
                backend_start=BACKEND_START,
                transaction_start=None,  # type: ignore[arg-type]
                state_change=STATE_CHANGE,
                state="idle in transaction",
                role_name="scm",
                application_name="psql",
            )

        index_target = IndexTarget(schema_name="public", index_name="orders_idx")
        with self.assertRaisesRegex(ValueError, "action_kind"):
            ActionPlan(
                action_kind=ActionKind.VACUUM,
                finding_fingerprint="fixture",
                target_kind=TargetKind.INDEX,
                target=index_target,
                target_key="fixture",
                command='VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) "public"."orders"',
                parameters=index_target.parameters,
                mode=Mode.DRY_RUN,
                reason="fixture",
            )

    def test_advisory_rules_never_create_destructive_plans(self) -> None:
        cases = (
            (RuleId.LONG_RUNNING_QUERY, TargetKind.BACKEND),
            (RuleId.CONNECTION_SATURATION, TargetKind.CLUSTER),
            (RuleId.UNUSED_LARGE_INDEX, TargetKind.INDEX),
        )

        for rule_id, target_kind in cases:
            with self.subTest(rule_id=rule_id.value):
                self.assertIsNone(
                    self.planner.plan(finding(rule_id, target_kind), HealingConfig(mode=Mode.ACT))
                )

    def test_manual_reindex_requires_feature_mode_confirmation_and_user_schema(self) -> None:
        disabled = HealingConfig(mode=Mode.ACT)
        dry_run = HealingConfig(mode=Mode.DRY_RUN, allow_manual_reindex=True)
        act = HealingConfig(mode=Mode.ACT, allow_manual_reindex=True)

        with self.assertRaisesRegex(ValueError, "allow_manual_reindex"):
            self.planner.plan_manual_reindex("public", "orders_idx", disabled, confirmed=True)
        with self.assertRaisesRegex(ValueError, "observe"):
            self.planner.plan_manual_reindex(
                "public", "orders_idx", HealingConfig(allow_manual_reindex=True), confirmed=True
            )
        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.planner.plan_manual_reindex("public", "orders_idx", act, confirmed=False)
        with self.assertRaisesRegex(ValueError, "system schema"):
            self.planner.plan_manual_reindex("pg_catalog", "pg_class_oid_index", act, confirmed=True)

        plan = self.planner.plan_manual_reindex("public", "orders_idx", dry_run, confirmed=False)

        self.assertEqual(plan.action_kind.value, "reindex_index")
        self.assertEqual(plan.target_kind, TargetKind.INDEX)
        self.assertEqual(
            plan.target,
            IndexTarget(schema_name="public", index_name="orders_idx"),
        )
        self.assertEqual(plan.command, 'REINDEX INDEX CONCURRENTLY "public"."orders_idx"')
        self.assertEqual(
            plan.parameters,
            {
                "schema": "public",
                "index": "orders_idx",
                "database_oid": None,
                "index_oid": None,
                "parent_relation_oid": None,
            },
        )
        self.assertEqual(plan.mode, Mode.DRY_RUN)
        self.assertEqual(plan.reason, "manual_reindex_requested")
