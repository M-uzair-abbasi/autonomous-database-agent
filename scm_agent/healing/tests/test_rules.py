"""Pure, deterministic anomaly-detection behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from scm_agent.healing.config import RulePolicy
from scm_agent.healing.models import (
    ActivitySample,
    CollectionError,
    ConnectionSample,
    HealthSnapshot,
    IndexStatsSample,
    RuleId,
    Severity,
    TableStatsSample,
)
from scm_agent.healing.rules import (
    detect_all,
    detect_connection_saturation,
    detect_idle_transactions,
    detect_long_queries,
    detect_table_pressure,
    detect_unused_indexes,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def table(*, live: int, dead: int, name: str = "orders") -> TableStatsSample:
    return TableStatsSample(
        observed_at=NOW,
        database_oid=17,
        relation_oid=42,
        schema_name="public",
        relation_name=name,
        n_live_tup=live,
        n_dead_tup=dead,
    )


def activity(**changes: object) -> ActivitySample:
    values: dict[str, object] = {
        "observed_at": NOW,
        "database_oid": 17,
        "database_name": "scm_demo",
        "pid": 123,
        "backend_start": NOW - timedelta(hours=1),
        "role_name": "scm",
        "application_name": "psql",
        "backend_type": "client backend",
        "state": "active",
        "state_change": NOW - timedelta(minutes=6),
        "transaction_start": NOW - timedelta(minutes=6),
        "query_start": NOW - timedelta(minutes=6),
    }
    values.update(changes)
    return ActivitySample(**values)  # type: ignore[arg-type]


def index(**changes: object) -> IndexStatsSample:
    values: dict[str, object] = {
        "observed_at": NOW,
        "database_oid": 17,
        "table_oid": 42,
        "index_oid": 99,
        "schema_name": "public",
        "table_name": "orders",
        "index_name": "orders_customer_idx",
        "index_size_bytes": 104_857_601,
        "idx_scan": 0,
        "stats_reset": NOW - timedelta(days=8),
        "server_started": NOW - timedelta(days=30),
    }
    values.update(changes)
    return IndexStatsSample(**values)  # type: ignore[arg-type]


class TablePressureTests(unittest.TestCase):
    def test_minimum_table_rows_boundary_is_strict(self) -> None:
        policy = RulePolicy()
        below_minimum_rows = table(live=4_998, dead=5_001)
        equal_minimum_rows = table(live=4_999, dead=5_001)
        above_minimum_rows = table(live=5_000, dead=5_001)

        self.assertEqual(detect_table_pressure((below_minimum_rows,), policy), ())
        self.assertEqual(detect_table_pressure((equal_minimum_rows,), policy), ())
        self.assertEqual(
            len(detect_table_pressure((above_minimum_rows,), policy)),
            1,
        )

    def test_requires_all_strict_thresholds_and_safe_denominator(self) -> None:
        policy = RulePolicy()
        at_ratio = table(live=20_000, dead=5_000)
        above_ratio = table(live=20_000, dead=5_001)
        below_dead = table(live=20_000, dead=4_999)
        zero_denominator = table(live=0, dead=0)

        self.assertEqual(detect_table_pressure((at_ratio,), policy), ())
        self.assertEqual(detect_table_pressure((below_dead,), policy), ())
        self.assertEqual(detect_table_pressure((zero_denominator,), policy), ())
        finding = detect_table_pressure((above_ratio,), policy)[0]
        self.assertEqual(finding.rule_id, RuleId.DEAD_TUPLE_PRESSURE)
        self.assertEqual(finding.severity, Severity.WARNING)
        self.assertTrue(finding.action_eligible)
        self.assertEqual(finding.target_key, "17:42")
        self.assertEqual(finding.evidence["dead_tuple_ratio"], 0.200032)
        self.assertNotIn("query", finding.evidence)

    def test_ignores_negative_catalog_counts(self) -> None:
        self.assertEqual(detect_table_pressure((table(live=-1, dead=6_000),), RulePolicy()), ())


class IdleTransactionTests(unittest.TestCase):
    def test_idle_transaction_warning_boundary_is_strict(self) -> None:
        policy = RulePolicy()
        below_warning = activity(
            state="idle in transaction",
            state_change=NOW - timedelta(minutes=4),
        )
        equal_warning = activity(
            pid=124,
            state="idle in transaction",
            state_change=NOW - timedelta(minutes=5),
        )

        self.assertEqual(detect_idle_transactions((below_warning,), policy), ())
        self.assertEqual(detect_idle_transactions((equal_warning,), policy), ())

    def test_warns_for_both_idle_transaction_states_but_only_exact_state_can_act(self) -> None:
        policy = RulePolicy()
        exact_old = activity(state="idle in transaction", state_change=NOW - timedelta(minutes=16))
        aborted_old = activity(
            pid=124,
            state="idle in transaction (aborted)",
            state_change=NOW - timedelta(minutes=16),
        )

        findings = detect_idle_transactions((exact_old, aborted_old), policy)

        self.assertEqual([finding.rule_id for finding in findings], [RuleId.IDLE_IN_TRANSACTION] * 2)
        self.assertEqual([finding.action_eligible for finding in findings], [True, False])
        self.assertEqual([finding.severity for finding in findings], [Severity.CRITICAL, Severity.WARNING])

    def test_uses_strict_age_and_excludes_future_system_and_self_sessions(self) -> None:
        at_action_threshold = activity(
            state="idle in transaction", state_change=NOW - timedelta(minutes=15)
        )
        future = activity(pid=124, state="idle in transaction", state_change=NOW + timedelta(seconds=1))
        worker = activity(pid=125, state="idle in transaction", backend_type="autovacuum worker")
        own = activity(pid=126, state="idle in transaction", is_self=True)

        self.assertFalse(detect_idle_transactions((at_action_threshold,), RulePolicy())[0].action_eligible)
        self.assertEqual(detect_idle_transactions((future, worker, own), RulePolicy()), ())


class LongQueryTests(unittest.TestCase):
    def test_long_query_boundary_and_missing_start_do_not_fire(self) -> None:
        policy = RulePolicy()
        below_threshold = activity(query_start=NOW - timedelta(minutes=4))
        equal_threshold = activity(pid=124, query_start=NOW - timedelta(minutes=5))
        missing_start = activity(pid=125, query_start=None)

        self.assertEqual(detect_long_queries((below_threshold,), policy), ())
        self.assertEqual(detect_long_queries((equal_threshold,), policy), ())
        self.assertEqual(detect_long_queries((missing_start,), policy), ())

    def test_only_active_old_queries_are_advisory_and_query_text_is_absent(self) -> None:
        old_active = activity(query_start=NOW - timedelta(minutes=6))
        idle = activity(pid=124, state="idle", query_start=NOW - timedelta(minutes=6))
        future = activity(pid=125, query_start=NOW + timedelta(seconds=1))
        self_session = activity(pid=126, is_self=True)

        finding = detect_long_queries((old_active,), RulePolicy())[0]

        self.assertEqual(finding.rule_id, RuleId.LONG_RUNNING_QUERY)
        self.assertEqual(finding.severity, Severity.WARNING)
        self.assertFalse(finding.action_eligible)
        self.assertNotIn("query", finding.evidence)
        self.assertEqual(detect_long_queries((idle, future, self_session), RulePolicy()), ())


class ConnectionSaturationTests(unittest.TestCase):
    def test_connection_critical_equality_remains_warning(self) -> None:
        policy = RulePolicy()
        exactly_critical = ConnectionSample(NOW, client_backend_count=90, maximum_connections=100)

        finding = detect_connection_saturation(exactly_critical, policy)[0]

        self.assertEqual(finding.severity, Severity.WARNING)

    def test_uses_warning_and_critical_bands_with_strict_boundaries(self) -> None:
        policy = RulePolicy()
        equal_warning = ConnectionSample(NOW, client_backend_count=80, maximum_connections=100)
        warning = ConnectionSample(NOW, client_backend_count=81, maximum_connections=100)
        critical = ConnectionSample(NOW, client_backend_count=91, maximum_connections=100)
        no_capacity = ConnectionSample(NOW, client_backend_count=10, maximum_connections=0)

        self.assertEqual(detect_connection_saturation(None, policy), ())
        self.assertEqual(detect_connection_saturation(equal_warning, policy), ())
        self.assertEqual(detect_connection_saturation(no_capacity, policy), ())
        self.assertEqual(detect_connection_saturation(warning, policy)[0].severity, Severity.WARNING)
        finding = detect_connection_saturation(critical, policy)[0]
        self.assertEqual(finding.severity, Severity.CRITICAL)
        self.assertFalse(finding.action_eligible)


class UnusedIndexTests(unittest.TestCase):
    def test_statistics_age_equality_and_scans_above_maximum_do_not_fire(self) -> None:
        policy = RulePolicy()
        exactly_minimum_age = index(
            stats_reset=NOW - timedelta(days=7),
            server_started=NOW - timedelta(days=30),
        )
        scans_above_maximum = index(index_oid=100, idx_scan=1)

        self.assertEqual(detect_unused_indexes((exactly_minimum_age,), policy), ())
        self.assertEqual(detect_unused_indexes((scans_above_maximum,), policy), ())

    def test_requires_large_unused_index_with_trustworthy_statistics(self) -> None:
        policy = RulePolicy()
        eligible = index()
        too_small = index(index_oid=100, index_size_bytes=104_857_600)
        reset_recently = index(index_oid=101, stats_reset=NOW - timedelta(days=6))
        reset_in_future = index(index_oid=102, stats_reset=NOW + timedelta(seconds=1))
        missing_window = index(index_oid=103, stats_reset=None, server_started=None)

        finding = detect_unused_indexes((eligible,), policy)[0]
        self.assertEqual(finding.rule_id, RuleId.UNUSED_LARGE_INDEX)
        self.assertEqual(finding.severity, Severity.WARNING)
        self.assertFalse(finding.action_eligible)
        self.assertEqual(finding.target_key, "17:99")
        self.assertEqual(
            detect_unused_indexes((too_small, reset_recently, reset_in_future, missing_window), policy), ()
        )

    def test_excludes_primary_unique_constraint_and_unhealthy_indexes(self) -> None:
        samples = (
            index(index_oid=100, is_primary=True),
            index(index_oid=101, is_unique=True),
            index(index_oid=102, is_constraint=True),
            index(index_oid=103, is_valid=False),
            index(index_oid=104, is_ready=False),
            index(index_oid=105, is_live=False),
            index(index_oid=106, idx_scan=-1),
        )

        self.assertEqual(detect_unused_indexes(samples, RulePolicy()), ())


class CombinedRuleTests(unittest.TestCase):
    def test_detect_all_suppresses_a_rule_group_when_that_collector_failed(self) -> None:
        snapshot = HealthSnapshot(
            observed_at=NOW,
            tables=(table(live=20_000, dead=5_001),),
            errors=(CollectionError("table", "permission denied", NOW),),
        )

        self.assertEqual(detect_all(snapshot, RulePolicy()), ())

    def test_detect_all_is_deterministic_by_rule_and_target_key(self) -> None:
        snapshot = HealthSnapshot(
            observed_at=NOW,
            activities=(activity(pid=200), activity(pid=100)),
            tables=(table(live=20_000, dead=5_001, name="z"),),
            indexes=(index(),),
            connection=ConnectionSample(NOW, 91, 100),
        )

        findings = detect_all(snapshot, RulePolicy())

        self.assertEqual(
            [(finding.rule_id.value, finding.target_key) for finding in findings],
            [
                ("connection_saturation", "cluster"),
                ("dead_tuple_pressure", "17:42"),
                ("long_running_query", "17:100"),
                ("long_running_query", "17:200"),
                ("unused_large_index", "17:99"),
            ],
        )
        self.assertEqual(len({finding.fingerprint for finding in findings}), len(findings))


if __name__ == "__main__":
    unittest.main()
