"""Dependency-free contracts for the PostgreSQL healing gateway."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import textwrap
from typing import Mapping
import unittest

from scm_agent.healing import postgres
from scm_agent.healing.actions import build_reindex_command, build_vacuum_command
from scm_agent.healing.config import HealingConfig
from scm_agent.healing.models import (
    ActionKind,
    ActionPlan,
    ActionStatus,
    BackendTarget,
    IndexTarget,
    Mode,
    TableTarget,
    TargetKind,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = routes
        self.result: object = []
        self.description = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: object, parameters: object = None) -> "FakeCursor":
        text = str(sql)
        for marker, result in self.routes.items():
            if marker in text:
                if isinstance(result, BaseException):
                    raise result
                self.result = result(parameters) if callable(result) else result
                return self
        if "set_config" in text:
            self.result = []
            return self
        raise AssertionError(f"unexpected fixed SQL: {text}")

    def fetchall(self) -> list[object]:
        return list(self.result)  # type: ignore[arg-type]

    def fetchone(self) -> object:
        values = list(self.result)  # type: ignore[arg-type]
        return values[0] if values else None


class FakeConnection:
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = routes
        self.closed = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.routes)


class FakeConnector:
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = routes
        self.connection_options: list[dict[str, object]] = []

    def __call__(self, _dsn: str, **options: object) -> FakeConnection:
        self.connection_options.append(dict(options))
        return FakeConnection(self.routes)


class FakeDatabaseError(RuntimeError):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("intentionally secret-looking fake database failure")
        self.sqlstate = sqlstate


class RouteSequence:
    def __init__(self, *results: object) -> None:
        self.results = list(results)

    def __call__(self, _parameters: object) -> object:
        if not self.results:
            raise AssertionError("fixed query executed more times than the scenario permits")
        return self.results.pop(0)


def server_facts(*, version: int = 160000, can_signal: bool = True) -> dict[str, object]:
    return {
        "server_version_num": version,
        "server_version": "16.4",
        "database_name": "scm_demo",
        "database_oid": 17,
        "role_name": "scm",
        "role_oid": 10,
        "is_superuser": False,
        "has_monitoring_visibility": True,
        "can_signal_backend": can_signal,
    }


def table_identity(**changes: object) -> dict[str, object]:
    row = {
        "database_oid": 17,
        "relation_oid": 42,
        "schema_name": "public",
        "relation_name": "orders",
        "relation_kind": "r",
        "persistence": "p",
        "owner_oid": 10,
        "current_role_oid": 10,
        "database_owner_oid": 20,
        "current_is_superuser": False,
    }
    row.update(changes)
    return row


def index_identity(**changes: object) -> dict[str, object]:
    row = {
        "database_oid": 17,
        "index_oid": 84,
        "parent_relation_oid": 42,
        "schema_name": "public",
        "index_name": "orders_customer_idx",
        "index_kind": "i",
        "index_persistence": "p",
        "parent_kind": "r",
        "owner_oid": 10,
        "current_role_oid": 10,
        "database_owner_oid": 20,
        "current_is_superuser": False,
        "is_valid": True,
        "is_ready": True,
        "is_live": True,
        "is_exclusion": False,
    }
    row.update(changes)
    return row


def backend_identity(**changes: object) -> dict[str, object]:
    row = {
        "database_oid": 17,
        "pid": 123,
        "backend_start": NOW - timedelta(hours=1),
        "transaction_start": NOW - timedelta(minutes=30),
        "state_change": NOW - timedelta(minutes=20),
        "state": "idle in transaction",
        "role_name": "app_user",
        "application_name": "psql",
        "backend_type": "client backend",
        "target_is_superuser": False,
        "current_pid": 999,
    }
    row.update(changes)
    return row


def table_plan(*, mode: Mode = Mode.ACT) -> ActionPlan:
    target = TableTarget(17, 42, "public", "orders")
    return ActionPlan(
        ActionKind.VACUUM,
        "table-fingerprint",
        TargetKind.TABLE,
        target,
        "17:42",
        build_vacuum_command("public", "orders"),
        target.parameters,
        mode,
        "fixture",
    )


def index_plan(*, mode: Mode = Mode.ACT) -> ActionPlan:
    target = IndexTarget("public", "orders_customer_idx")
    return ActionPlan(
        ActionKind.REINDEX_INDEX,
        "index-fingerprint",
        TargetKind.INDEX,
        target,
        "manual-index",
        build_reindex_command("public", "orders_customer_idx"),
        target.parameters,
        mode,
        "fixture",
    )


def backend_plan() -> ActionPlan:
    target = BackendTarget(
        database_oid=17,
        pid=123,
        backend_start=NOW - timedelta(hours=1),
        transaction_start=NOW - timedelta(minutes=30),
        state_change=NOW - timedelta(minutes=20),
        state="idle in transaction",
        role_name="app_user",
        application_name="psql",
    )
    return ActionPlan(
        ActionKind.TERMINATE_BACKEND,
        "backend-fingerprint",
        TargetKind.BACKEND,
        target,
        "17:123",
        "SELECT pg_terminate_backend(?)",
        target.parameters,
        Mode.ACT,
        "fixture",
    )


class LazyDriverAndSqlContractTests(unittest.TestCase):
    def test_isolated_import_is_lazy_and_live_load_has_safe_driver_error(self) -> None:
        project_root = Path(__file__).resolve().parents[3]
        script = textwrap.dedent(
            """
            import builtins

            attempted = []
            real_import = builtins.__import__

            def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "psycopg" or name.startswith("psycopg."):
                    attempted.append(name)
                    raise ImportError("forced missing psycopg for lazy-import contract")
                return real_import(name, globals, locals, fromlist, level)

            builtins.__import__ = blocked_import
            import scm_agent.healing.postgres as postgres
            assert attempted == [], attempted

            try:
                postgres._load_psycopg()
            except postgres.PsycopgUnavailableError as error:
                assert 'pip install "psycopg[binary]"' in str(error)
            else:
                raise AssertionError("live driver load did not fail safely")
            assert attempted == ["psycopg"], attempted
            """
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_activity_collector_is_catalog_qualified_and_excludes_self_and_workers(self) -> None:
        normalized = " ".join(postgres.ACTIVITY_SQL.lower().split())

        self.assertIn("pg_catalog.pg_stat_activity", normalized)
        self.assertIn("pg_catalog.pg_backend_pid()", normalized)
        self.assertIn("backend_type = 'client backend'", normalized)
        self.assertIn("datid =", normalized)

    def test_saturation_collector_is_cluster_wide(self) -> None:
        normalized = " ".join(postgres.SATURATION_SQL.lower().split())

        self.assertIn("pg_catalog.pg_stat_activity", normalized)
        self.assertIn("backend_type = 'client backend'", normalized)
        self.assertNotIn("current_database", normalized)
        self.assertNotIn("where datid", normalized)

    def test_relation_collectors_use_only_catalog_qualified_sources(self) -> None:
        table_sql = " ".join(postgres.TABLE_STATS_SQL.lower().split())
        index_sql = " ".join(postgres.INDEX_STATS_SQL.lower().split())

        self.assertIn("pg_catalog.pg_stat_user_tables", table_sql)
        self.assertIn("pg_catalog.pg_stat_user_indexes", index_sql)
        self.assertIn("pg_catalog.pg_index", index_sql)
        self.assertIn("pg_catalog.pg_constraint", index_sql)

    def test_fixed_sql_does_not_schema_qualify_current_user_keyword(self) -> None:
        statements = (
            postgres.SERVER_FACTS_SQL,
            postgres.TABLE_TARGET_SQL,
            postgres.INDEX_TARGET_SQL,
            postgres.MAINTAIN_PRIVILEGE_SQL,
        )
        for statement in statements:
            with self.subTest(marker=statement.splitlines()[1].strip()):
                self.assertNotIn("pg_catalog.current_user", statement.lower())


class CollectorMappingTests(unittest.TestCase):
    def _routes(self) -> dict[str, object]:
        return {
            "scm-ovs:activity": [{
                "observed_at": NOW,
                "database_oid": 17,
                "database_name": "scm_demo",
                "pid": 123,
                "backend_start": NOW - timedelta(hours=1),
                "role_name": "scm",
                "application_name": "psql",
                "backend_type": "client backend",
                "state": "idle in transaction",
                "state_change": NOW - timedelta(minutes=20),
                "transaction_start": NOW - timedelta(minutes=30),
                "query_start": NOW - timedelta(minutes=30),
                "wait_event_type": "Client",
                "wait_event": "ClientRead",
                "is_self": False,
            }],
            "scm-ovs:tables": [{
                "observed_at": NOW,
                "database_oid": 17,
                "relation_oid": 42,
                "schema_name": "public",
                "relation_name": "orders",
                "n_live_tup": 10_000,
                "n_dead_tup": 6_000,
                "last_autovacuum": None,
                "last_vacuum": None,
                "vacuum_count": 1,
                "autovacuum_count": 2,
                "relation_size_bytes": 8192,
            }],
            "scm-ovs:indexes": [{
                "observed_at": NOW,
                "database_oid": 17,
                "table_oid": 42,
                "index_oid": 84,
                "schema_name": "public",
                "table_name": "orders",
                "index_name": "orders_customer_idx",
                "index_size_bytes": 16384,
                "idx_scan": 0,
                "is_valid": True,
                "is_ready": True,
                "is_live": True,
                "is_unique": False,
                "is_primary": False,
                "is_constraint": False,
                "stats_reset": NOW - timedelta(days=30),
                "server_started": NOW - timedelta(days=31),
            }],
            "scm-ovs:saturation": [{
                "observed_at": NOW,
                "client_backend_count": 81,
                "maximum_connections": 100,
                "reserved_connections": 3,
            }],
        }

    def test_collect_health_maps_complete_rows_and_uses_read_only_monitor_session(self) -> None:
        connector = FakeConnector(self._routes())
        gateway = postgres.PostgresGateway(HealingConfig(), connect=connector, clock=lambda: NOW)

        snapshot = gateway.collect_health()

        self.assertEqual(snapshot.observed_at, NOW)
        self.assertEqual(snapshot.activities[0].pid, 123)
        self.assertEqual(snapshot.activities[0].transaction_start, NOW - timedelta(minutes=30))
        self.assertEqual(snapshot.tables[0].relation_oid, 42)
        self.assertEqual(snapshot.indexes[0].index_oid, 84)
        self.assertEqual(snapshot.connection.client_backend_count, 81)  # type: ignore[union-attr]
        self.assertEqual(snapshot.errors, ())
        self.assertEqual(
            connector.connection_options,
            [{
                "application_name": "scm-ovs/monitor",
                "autocommit": True,
                "connect_timeout": 5,
            }],
        )

    def test_malformed_required_row_becomes_collection_error(self) -> None:
        routes = self._routes()
        malformed = dict(routes["scm-ovs:activity"][0])  # type: ignore[index]
        malformed["database_oid"] = None
        routes["scm-ovs:activity"] = [malformed]
        gateway = postgres.PostgresGateway(
            HealingConfig(), connect=FakeConnector(routes), clock=lambda: NOW
        )

        snapshot = gateway.collect_health()

        self.assertEqual(snapshot.activities, ())
        self.assertEqual(len(snapshot.errors), 1)
        self.assertEqual(snapshot.errors[0].collector, "activity")
        self.assertNotIn("NoneType", snapshot.errors[0].message)


class ActionGateTests(unittest.TestCase):
    def _gateway(
        self,
        *,
        target_marker: str,
        target_row: Mapping[str, object] | None,
        facts: Mapping[str, object] | None = None,
        extra: Mapping[str, object] | None = None,
        config: HealingConfig | None = None,
    ) -> tuple[postgres.PostgresGateway, FakeConnector]:
        routes: dict[str, object] = {
            "scm-ovs:server-facts": [dict(facts or server_facts())],
            target_marker: [] if target_row is None else [dict(target_row)],
            "scm-ovs:advisory-lock": [{"locked": True}],
            "scm-ovs:advisory-unlock": [{"unlocked": True}],
            "scm-ovs:maintain-privilege": [{"has_maintain": False}],
        }
        if extra:
            routes.update(extra)
        connector = FakeConnector(routes)
        gateway = postgres.PostgresGateway(
            config or HealingConfig(
                mode=Mode.ACT,
                allow_terminate_idle_transaction=True,
                allow_manual_reindex=True,
            ),
            connect=connector,
            clock=lambda: NOW,
        )
        return gateway, connector

    def test_runtime_observe_rejects_stale_act_plan_before_database_io(self) -> None:
        gateway, connector = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=table_identity(),
            config=HealingConfig(mode=Mode.OBSERVE),
            extra={"VACUUM (": AssertionError("observe gateway dispatched VACUUM")},
        )

        decision = gateway.check_plan(table_plan(mode=Mode.ACT))
        result = gateway.execute(table_plan(mode=Mode.ACT))

        self.assertEqual((decision.allowed, decision.reason), (False, "gateway_observe_mode"))
        self.assertEqual(result.status, ActionStatus.PRECONDITION_REJECTED)
        self.assertEqual(result.evidence["reason"], "gateway_observe_mode")
        self.assertEqual(connector.connection_options, [])

    def test_runtime_dry_run_rejects_stale_act_plan_before_database_io(self) -> None:
        gateway, connector = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=table_identity(),
            config=HealingConfig(mode=Mode.DRY_RUN),
            extra={"VACUUM (": AssertionError("dry-run gateway dispatched VACUUM")},
        )

        decision = gateway.check_plan(table_plan(mode=Mode.ACT))
        result = gateway.execute(table_plan(mode=Mode.ACT))

        self.assertEqual((decision.allowed, decision.reason), (False, "gateway_mode_mismatch"))
        self.assertEqual(result.status, ActionStatus.PRECONDITION_REJECTED)
        self.assertEqual(result.evidence["reason"], "gateway_mode_mismatch")
        self.assertEqual(connector.connection_options, [])

    def test_runtime_act_rejects_dry_run_plan_before_database_io(self) -> None:
        gateway, connector = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=table_identity(),
            config=HealingConfig(mode=Mode.ACT),
            extra={"VACUUM (": AssertionError("act gateway accepted stale dry-run plan")},
        )

        decision = gateway.check_plan(table_plan(mode=Mode.DRY_RUN))
        result = gateway.execute(table_plan(mode=Mode.DRY_RUN))

        self.assertEqual((decision.allowed, decision.reason), (False, "gateway_mode_mismatch"))
        self.assertEqual(result.status, ActionStatus.PRECONDITION_REJECTED)
        self.assertEqual(result.evidence["reason"], "gateway_mode_mismatch")
        self.assertEqual(connector.connection_options, [])

    def test_disabled_high_risk_flags_reject_stale_plans_before_database_io(self) -> None:
        cases = (
            (backend_plan(), "terminate_backend_disabled", "scm-ovs:terminate-backend"),
            (index_plan(), "manual_reindex_disabled", "REINDEX INDEX CONCURRENTLY"),
        )
        for plan, reason, action_marker in cases:
            with self.subTest(action=plan.action_kind.value):
                gateway, connector = self._gateway(
                    target_marker="scm-ovs:backend-target",
                    target_row=backend_identity(),
                    config=HealingConfig(mode=Mode.ACT),
                    extra={action_marker: AssertionError("disabled action SQL was dispatched")},
                )

                decision = gateway.check_plan(plan)
                result = gateway.execute(plan)

                self.assertEqual((decision.allowed, decision.reason), (False, reason))
                self.assertEqual(result.status, ActionStatus.PRECONDITION_REJECTED)
                self.assertEqual(result.evidence["reason"], reason)
                self.assertEqual(connector.connection_options, [])

    def test_policy_denial_is_structured_and_does_not_leak_dsn(self) -> None:
        dsn = "postgresql://scm:top-secret@localhost:5434/scm_demo"
        gateway = postgres.PostgresGateway(
            HealingConfig(database_dsn=dsn, mode=Mode.OBSERVE),
            connect=FakeConnector({}),
            clock=lambda: NOW,
        )

        decision = gateway.check_plan(table_plan())

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "gateway_observe_mode")
        self.assertNotIn("top-secret", repr(decision))
        self.assertNotIn(dsn, repr(decision))

    def test_relation_identity_type_and_persistence_changes_are_rejected(self) -> None:
        cases = (
            (None, "stale_relation_identity"),
            (table_identity(relation_oid=99), "stale_relation_identity"),
            (table_identity(relation_name="renamed"), "stale_relation_identity"),
            (table_identity(relation_kind="v"), "unsupported_relation_type"),
            (table_identity(schema_name="pg_catalog"), "system_relation_rejected"),
            (table_identity(persistence="t"), "temporary_relation_rejected"),
        )

        for row, expected in cases:
            with self.subTest(expected=expected, row=row):
                gateway, _ = self._gateway(target_marker="scm-ovs:table-target", target_row=row)
                decision = gateway.check_plan(table_plan())
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, expected)

    def test_postgresql_below_twelve_is_rejected(self) -> None:
        gateway, _ = self._gateway(
            target_marker="scm-ovs:index-target",
            target_row=index_identity(),
            facts=server_facts(version=110021),
        )

        decision = gateway.check_plan(index_plan())

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unsupported_server_version")

    def test_ineligible_index_catalog_states_are_rejected(self) -> None:
        cases = (
            (index_identity(index_kind="I"), "unsupported_relation_type"),
            (index_identity(index_persistence="t"), "temporary_relation_rejected"),
            (index_identity(schema_name="pg_catalog"), "system_relation_rejected"),
            (index_identity(is_exclusion=True), "exclusion_index_rejected"),
            (index_identity(is_valid=False), "ineligible_index_state"),
            (index_identity(is_ready=False), "ineligible_index_state"),
            (index_identity(is_live=False), "ineligible_index_state"),
        )
        for row, reason in cases:
            with self.subTest(reason=reason):
                gateway, _ = self._gateway(
                    target_marker="scm-ovs:index-target", target_row=row
                )
                decision = gateway.check_plan(index_plan())
                self.assertEqual((decision.allowed, decision.reason), (False, reason))

    def test_vacuum_authority_is_version_aware(self) -> None:
        unowned = table_identity(owner_oid=30, current_role_oid=10, database_owner_oid=20)
        gateway16, _ = self._gateway(target_marker="scm-ovs:table-target", target_row=unowned)
        gateway17, _ = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=unowned,
            facts=server_facts(version=170000),
            extra={"scm-ovs:maintain-privilege": [{"has_maintain": True}]},
        )

        denied = gateway16.check_plan(table_plan())
        allowed = gateway17.check_plan(table_plan())

        self.assertEqual((denied.allowed, denied.reason), (False, "insufficient_privilege"))
        self.assertEqual((allowed.allowed, allowed.reason), (True, "ok"))

    def test_postgresql_seventeen_maintain_authority_allows_concurrent_reindex(self) -> None:
        unowned = index_identity(owner_oid=30, current_role_oid=10, database_owner_oid=20)
        gateway, _ = self._gateway(
            target_marker="scm-ovs:index-target",
            target_row=unowned,
            facts=server_facts(version=170000),
            extra={"scm-ovs:maintain-privilege": [{"has_maintain": True}]},
        )

        decision = gateway.check_plan(index_plan())

        self.assertEqual((decision.allowed, decision.reason), (True, "ok"))

    def test_backend_full_fingerprint_and_protection_gates(self) -> None:
        cases = (
            (backend_identity(current_pid=123), None, "self_backend_rejected"),
            (backend_identity(backend_start=NOW - timedelta(minutes=2)), None, "stale_backend_identity"),
            (backend_identity(state_change=NOW - timedelta(minutes=19)), None, "stale_backend_identity"),
            (backend_identity(role_name="postgres"), None, "protected_role"),
            (backend_identity(target_is_superuser=True), None, "superuser_target_rejected"),
            (backend_identity(), server_facts(can_signal=False), "insufficient_privilege"),
        )

        for row, facts, expected in cases:
            with self.subTest(expected=expected):
                gateway, _ = self._gateway(
                    target_marker="scm-ovs:backend-target",
                    target_row=row,
                    facts=facts,
                )
                decision = gateway.check_plan(backend_plan())
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, expected)

    def test_advisory_lock_failure_returns_nonexecuting_result(self) -> None:
        gateway, _ = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=table_identity(),
            extra={"scm-ovs:advisory-lock": [{"locked": False}]},
        )

        result = gateway.execute(table_plan())

        self.assertEqual(result.status, ActionStatus.PRECONDITION_REJECTED)
        self.assertEqual(result.evidence["reason"], "advisory_lock_unavailable")

    def test_identity_is_revalidated_again_after_session_lock(self) -> None:
        gateway, _ = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=table_identity(),
            extra={
                "scm-ovs:table-target": RouteSequence(
                    [table_identity()], [table_identity(relation_name="renamed")]
                ),
                "VACUUM (": AssertionError("stale relation was vacuumed"),
            },
        )

        result = gateway.execute(table_plan())

        self.assertEqual(result.status, ActionStatus.PRECONDITION_REJECTED)
        self.assertEqual(result.evidence["reason"], "stale_relation_identity")

    def test_dry_run_revalidates_but_never_executes_command(self) -> None:
        gateway, _ = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=table_identity(),
            config=HealingConfig(mode=Mode.DRY_RUN),
            extra={"VACUUM (": AssertionError("dry-run executed VACUUM")},
        )

        result = gateway.execute(table_plan(mode=Mode.DRY_RUN))

        self.assertEqual(result.status, ActionStatus.SIMULATED)
        self.assertEqual(result.evidence["reason"], "dry_run")

    def test_action_errors_are_classified_by_sqlstate_without_leaking_exception_text(self) -> None:
        cases = (
            ("42501", ActionStatus.FAILED, "insufficient_privilege"),
            ("55P03", ActionStatus.FAILED, "lock_timeout"),
            ("57014", ActionStatus.FAILED, "statement_timeout"),
            ("08006", ActionStatus.OUTCOME_UNKNOWN, "connection_lost"),
        )

        for sqlstate, status, reason in cases:
            with self.subTest(sqlstate=sqlstate):
                gateway, _ = self._gateway(
                    target_marker="scm-ovs:table-target",
                    target_row=table_identity(),
                    extra={"VACUUM (": FakeDatabaseError(sqlstate)},
                )
                result = gateway.execute(table_plan())
                self.assertEqual(result.status, status)
                self.assertEqual(result.evidence["reason"], reason)
                self.assertNotIn("secret-looking", result.error or "")

    def test_termination_uses_one_full_conditional_statement_and_classifies_rows(self) -> None:
        normalized = " ".join(postgres.TERMINATE_BACKEND_SQL.lower().split())
        for fingerprint in (
            "datid = %s",
            "pid = %s",
            "backend_start = %s",
            "xact_start = %s",
            "state_change = %s",
            "state = %s",
            "usename = %s",
            "application_name = %s",
            "backend_type = 'client backend'",
        ):
            self.assertIn(fingerprint, normalized)
        self.assertEqual(normalized.count("pg_catalog.pg_terminate_backend"), 1)
        self.assertIn("target.usename::text <> all(%s::text[])", normalized)

        outcomes = (
            ([], ActionStatus.PRECONDITION_REJECTED, "stale_backend_identity"),
            ([{"terminated": False}], ActionStatus.FAILED, "termination_returned_false"),
            ([{"terminated": True}], ActionStatus.SUCCEEDED, "action_succeeded"),
        )
        for rows, status, reason in outcomes:
            with self.subTest(rows=rows):
                gateway, _ = self._gateway(
                    target_marker="scm-ovs:backend-target",
                    target_row=backend_identity(),
                    extra={"scm-ovs:terminate-backend": rows},
                )
                result = gateway.execute(backend_plan())
                self.assertEqual(result.status, status)
                self.assertEqual(result.evidence["reason"], reason)

    def test_action_connection_is_dedicated_autocommit_and_timeouts_are_bounded(self) -> None:
        gateway, connector = self._gateway(
            target_marker="scm-ovs:table-target",
            target_row=table_identity(),
            extra={"VACUUM (": []},
        )

        result = gateway.execute(table_plan())

        self.assertEqual(result.status, ActionStatus.SUCCEEDED)
        self.assertEqual(
            connector.connection_options,
            [{
                "application_name": "scm-ovs/healer",
                "autocommit": True,
                "connect_timeout": 5,
            }],
        )


class CapabilityAndSnapshotTests(unittest.TestCase):
    def test_doctor_returns_secret_free_capability_report(self) -> None:
        secret = "postgresql://scm:top-secret@localhost:5434/scm_demo"
        connector = FakeConnector({"scm-ovs:server-facts": [server_facts()]})
        gateway = postgres.PostgresGateway(
            HealingConfig(database_dsn=secret), connect=connector, clock=lambda: NOW
        )

        report = gateway.doctor()

        self.assertEqual(report["driver_available"], True)
        self.assertEqual(report["database_reachable"], True)
        self.assertEqual(report["server_version_num"], 160000)
        self.assertNotIn("top-secret", repr(report))
        self.assertNotIn(secret, repr(report))

    def test_capture_before_and_after_return_fixed_target_snapshots(self) -> None:
        snapshot = {
            "observed_at": NOW,
            "database_oid": 17,
            "relation_oid": 42,
            "schema_name": "public",
            "relation_name": "orders",
            "n_live_tup": 10000,
            "n_dead_tup": 6000,
            "last_vacuum": None,
            "last_autovacuum": None,
            "vacuum_count": 1,
            "autovacuum_count": 2,
            "relation_size_bytes": 8192,
        }
        connector = FakeConnector({"scm-ovs:table-snapshot": [snapshot]})
        gateway = postgres.PostgresGateway(
            HealingConfig(), connect=connector, clock=lambda: NOW
        )

        before = gateway.capture_before(table_plan())
        after = gateway.capture_after(table_plan())

        self.assertEqual(before["present"], True)
        self.assertEqual(before["relation_oid"], 42)
        self.assertEqual(before["n_dead_tup"], 6000)
        self.assertEqual(after, before)

    def test_absent_backend_after_snapshot_is_an_explicit_nonsecret_value(self) -> None:
        gateway = postgres.PostgresGateway(
            HealingConfig(),
            connect=FakeConnector({"scm-ovs:backend-snapshot": []}),
            clock=lambda: NOW,
        )

        after = gateway.capture_after(backend_plan())

        self.assertEqual(after, {"present": False, "target_kind": "backend"})


if __name__ == "__main__":
    unittest.main()
