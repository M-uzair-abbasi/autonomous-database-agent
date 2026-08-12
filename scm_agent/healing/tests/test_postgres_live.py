"""Opt-in PostgreSQL integration tests.

The default suite never opens a network connection.  Set ``SCM_TEST_PG_DSN``
to enable read-only tests.  Schema/session-changing tests additionally require
``SCM_TEST_PG_DISPOSABLE=1`` and an exact database-name match with
``SCM_TEST_PG_DISPOSABLE_DB`` (default: ``scm_demo``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import re
from urllib.parse import unquote, urlparse
import unittest

from scm_agent.healing.actions import build_reindex_command, build_vacuum_command
from scm_agent.healing.config import HealingConfig, RulePolicy
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
from scm_agent.healing.postgres import PostgresGateway, _driver_is_available, _load_psycopg


DSN = os.environ.get("SCM_TEST_PG_DSN", "")
DISPOSABLE_NAME = os.environ.get("SCM_TEST_PG_DISPOSABLE_DB", "scm_demo")


def _database_name(dsn: str) -> str | None:
    if dsn.startswith(("postgresql://", "postgres://")):
        path = urlparse(dsn).path.lstrip("/")
        return unquote(path) if path else None
    match = re.search(r"(?:^|\s)(?:dbname|database)=([^\s]+)", dsn)
    return match.group(1).strip("'\"") if match else None


LIVE_ENABLED = bool(DSN)
DESTRUCTIVE_ENABLED = (
    LIVE_ENABLED
    and os.environ.get("SCM_TEST_PG_DISPOSABLE") == "1"
    and _database_name(DSN) == DISPOSABLE_NAME
)


@unittest.skipUnless(LIVE_ENABLED, "set SCM_TEST_PG_DSN to run PostgreSQL live tests")
class ReadOnlyLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _driver_is_available():
            self.skipTest('psycopg unavailable; install with pip install "psycopg[binary]"')
        self.gateway = PostgresGateway(HealingConfig(database_dsn=DSN))

    def test_collectors_and_doctor_execute_against_catalogs(self) -> None:
        snapshot = self.gateway.collect_health()
        report = self.gateway.doctor()

        self.assertEqual(snapshot.errors, ())
        self.assertIsNotNone(snapshot.connection)
        self.assertTrue(report["database_reachable"])
        self.assertGreaterEqual(report["server_version_num"], 120000)
        self.assertNotIn(DSN, repr(report))

    def test_observe_collection_does_not_change_schema_object_count(self) -> None:
        psycopg = _load_psycopg()
        with psycopg.connect(DSN, autocommit=True, application_name="scm-ovs/live-test") as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class "
                    "WHERE relnamespace NOT IN ("
                    "SELECT oid FROM pg_catalog.pg_namespace WHERE nspname LIKE 'pg_%')"
                )
                before = cursor.fetchone()[0]
            self.gateway.collect_health()
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class "
                    "WHERE relnamespace NOT IN ("
                    "SELECT oid FROM pg_catalog.pg_namespace WHERE nspname LIKE 'pg_%')"
                )
                after = cursor.fetchone()[0]
        self.assertEqual(after, before)


@unittest.skipUnless(
    DESTRUCTIVE_ENABLED,
    "requires SCM_TEST_PG_DISPOSABLE=1 and exact disposable database-name guard",
)
class DisposableActionLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _driver_is_available():
            self.skipTest('psycopg unavailable; install with pip install "psycopg[binary]"')
        self.psycopg = _load_psycopg()
        self.gateway = PostgresGateway(HealingConfig(database_dsn=DSN, mode=Mode.ACT))

    def _relation_identity(self, name: str) -> tuple[int, int]:
        with self.psycopg.connect(DSN, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT database.oid, relation.oid "
                    "FROM pg_catalog.pg_database AS database "
                    "JOIN pg_catalog.pg_class AS relation ON relation.relname = %s "
                    "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE database.datname = pg_catalog.current_database() "
                    "AND namespace.nspname = 'public'",
                    (name,),
                )
                row = cursor.fetchone()
                assert row is not None
                return row[0], row[1]

    def test_online_vacuum_runs_on_autocommit_action_connection(self) -> None:
        name = "scm_ovs_live_vacuum"
        with self.psycopg.connect(DSN, autocommit=True) as setup:
            try:
                with setup.cursor() as cursor:
                    cursor.execute(f'DROP TABLE IF EXISTS public."{name}"')
                    cursor.execute(f'CREATE TABLE public."{name}" (id integer)')
                    cursor.execute(f'INSERT INTO public."{name}" SELECT generate_series(1, 100)')
                    cursor.execute(f'DELETE FROM public."{name}" WHERE id <= 50')
                database_oid, relation_oid = self._relation_identity(name)
                target = TableTarget(database_oid, relation_oid, "public", name)
                plan = ActionPlan(
                    ActionKind.VACUUM,
                    "live-vacuum",
                    TargetKind.TABLE,
                    target,
                    f"{database_oid}:{relation_oid}",
                    build_vacuum_command("public", name),
                    target.parameters,
                    Mode.ACT,
                    "live_test",
                )
                result = self.gateway.execute(plan)
                self.assertEqual(result.status, ActionStatus.SUCCEEDED)
            finally:
                with setup.cursor() as cursor:
                    cursor.execute(f'DROP TABLE IF EXISTS public."{name}"')

    def test_exact_idle_session_termination(self) -> None:
        victim = self.psycopg.connect(DSN, application_name="scm-ovs/live-victim")
        try:
            with victim.cursor() as cursor:
                cursor.execute("SELECT 1")
            with self.psycopg.connect(DSN, autocommit=True) as observer:
                with observer.cursor() as cursor:
                    cursor.execute(
                        "SELECT datid, pid, backend_start, xact_start, state_change, state, "
                        "usename, application_name FROM pg_catalog.pg_stat_activity "
                        "WHERE pid = %s AND state = 'idle in transaction'",
                        (victim.info.backend_pid,),
                    )
                    row = cursor.fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            target = BackendTarget(
                database_oid=row[0], pid=row[1], backend_start=row[2], transaction_start=row[3],
                state_change=row[4], state=row[5], role_name=row[6], application_name=row[7]
            )
            plan = ActionPlan(
                ActionKind.TERMINATE_BACKEND, "live-backend", TargetKind.BACKEND, target,
                f"{row[0]}:{row[1]}", "SELECT pg_terminate_backend(?)", target.parameters,
                Mode.ACT, "live_test",
            )
            tiny_policy = RulePolicy(
                idle_transaction_warning=timedelta(microseconds=1),
                idle_transaction_action=timedelta(microseconds=1),
            )
            gateway = PostgresGateway(
                HealingConfig(
                    database_dsn=DSN,
                    mode=Mode.ACT,
                    policy=tiny_policy,
                    allow_terminate_idle_transaction=True,
                )
            )
            result = gateway.execute(plan)
            self.assertEqual(result.status, ActionStatus.SUCCEEDED)
        finally:
            victim.close()

    def test_explicit_concurrent_reindex(self) -> None:
        table_name = "scm_ovs_live_reindex"
        index_name = "scm_ovs_live_reindex_idx"
        with self.psycopg.connect(DSN, autocommit=True) as setup:
            try:
                with setup.cursor() as cursor:
                    cursor.execute(f'DROP TABLE IF EXISTS public."{table_name}"')
                    cursor.execute(f'CREATE TABLE public."{table_name}" (id integer)')
                    cursor.execute(f'CREATE INDEX "{index_name}" ON public."{table_name}" (id)')
                target = IndexTarget("public", index_name)
                plan = ActionPlan(
                    ActionKind.REINDEX_INDEX,
                    "live-reindex",
                    TargetKind.INDEX,
                    target,
                    "live-reindex",
                    build_reindex_command("public", index_name),
                    target.parameters,
                    Mode.ACT,
                    "live_test",
                )
                result = self.gateway.execute(plan)
                self.assertEqual(result.status, ActionStatus.SUCCEEDED)
            finally:
                with setup.cursor() as cursor:
                    cursor.execute(f'DROP TABLE IF EXISTS public."{table_name}"')


if __name__ == "__main__":
    unittest.main()
