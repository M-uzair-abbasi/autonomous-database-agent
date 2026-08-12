"""Disposable demo helpers must be narrow, tagged, bounded, and lazy."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scm_agent.healing.config import HealingConfig
from scm_agent.healing.demo import (
    DEMO_PREFIX,
    MAX_PRESSURE_CONNECTIONS,
    DemoGuardError,
    DemoRunner,
    ensure_disposable_target,
)


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    def execute(self, sql: object, parameters: object = None) -> "_Cursor":
        self.connection.statements.append((str(sql), parameters))
        return self

    def fetchone(self) -> tuple[int]:
        return (self.connection.database_name,)  # type: ignore[return-value]

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Connection:
    def __init__(
        self,
        application_name: str,
        autocommit: bool,
        database_name: str = "scm_ovs_demo_viva",
    ) -> None:
        self.application_name = application_name
        self.autocommit = autocommit
        self.database_name = database_name
        self.statements: list[tuple[str, object]] = []
        self.closed = False
        self.rolled_back = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class _Connector:
    def __init__(
        self,
        *,
        database_name: str = "scm_ovs_demo_viva",
        fail_on_call: int | None = None,
    ) -> None:
        self.connections: list[_Connection] = []
        self.database_name = database_name
        self.fail_on_call = fail_on_call
        self.calls = 0

    def __call__(self, dsn: str, **kwargs: object) -> _Connection:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise OSError("connection unavailable")
        connection = _Connection(
            str(kwargs["application_name"]),
            bool(kwargs["autocommit"]),
            self.database_name,
        )
        self.connections.append(connection)
        return connection


class DemoGuardTests(unittest.TestCase):
    def test_non_disposable_database_requires_current_explicit_opt_in(self) -> None:
        production = "postgresql://demo:secret@db.invalid/production"
        with self.assertRaises(DemoGuardError):
            ensure_disposable_target(production, {})
        ensure_disposable_target(production, {"SCM_HEALING_ALLOW_DEMO": "1"})

    def test_disposable_database_name_is_accepted_without_override(self) -> None:
        ensure_disposable_target(
            "postgresql://demo:secret@localhost/scm_ovs_demo_viva",
            {},
        )
        ensure_disposable_target("dbname=scm_ovs_demo_viva host=localhost", {})

    def test_unparseable_and_lookalike_database_names_fail_closed(self) -> None:
        for dsn in ("not-a-dsn", "postgresql://localhost/not_scm_ovs_demo_viva"):
            with self.subTest(dsn=dsn):
                with self.assertRaises(DemoGuardError):
                    ensure_disposable_target(dsn, {})

    def test_ambiguous_uri_database_specs_and_encoding_fail_closed(self) -> None:
        adversarial = (
            "postgresql://localhost/scm_ovs_demo_viva?dbname=production",
            "postgresql://localhost/scm_ovs_demo_viva?DATABASE=production",
            "postgresql://localhost/scm_ovs_demo_viva?%64bname=production",
            "postgresql://localhost/scm_ovs_demo_viva?database=scm_ovs_demo_viva%00production",
            "postgresql://localhost/%73cm_ovs_demo_viva",
            "postgresql://localhost/scm_ovs_demo_viva#production",
        )
        for dsn in adversarial:
            with self.subTest(dsn=dsn):
                with self.assertRaises(DemoGuardError):
                    ensure_disposable_target(dsn, {})

    def test_duplicate_or_unsafe_keyword_database_specs_fail_closed(self) -> None:
        adversarial = (
            "dbname=scm_ovs_demo_viva dbname=production host=localhost",
            "DBNAME=scm_ovs_demo_viva database=production host=localhost",
            "dbname='scm_ovs_demo_viva' host=localhost",
            r"dbname=scm_ovs_demo_viva\ host=production",
        )
        for dsn in adversarial:
            with self.subTest(dsn=dsn):
                with self.assertRaises(DemoGuardError):
                    ensure_disposable_target(dsn, {})


class DemoRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.connector = _Connector()
        self.config = HealingConfig(
            database_dsn="postgresql://demo:secret@localhost/scm_ovs_demo_viva",
            audit_path=Path(self.tempdir.name) / "audit.sqlite3",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def runner(self, *, wait: object = lambda seconds: None) -> DemoRunner:
        return DemoRunner(
            self.config,
            environ={},
            connect=self.connector,
            wait=wait,  # type: ignore[arg-type]
        )

    def test_bloat_setup_is_idempotent_and_disables_autovacuum_only_on_demo_table(self) -> None:
        result = self.runner().setup_bloat(rows=1_000)
        statements = self.connector.connections[0].statements
        sql = "\n".join(statement for statement, _ in statements)
        self.assertEqual(result["rows_requested"], 1_000)
        self.assertIn('CREATE TABLE IF NOT EXISTS "scm_ovs_demo_bloat"', sql)
        self.assertIn('TRUNCATE TABLE "scm_ovs_demo_bloat"', sql)
        self.assertIn(
            'ALTER TABLE "scm_ovs_demo_bloat" SET (autovacuum_enabled = false)',
            sql,
        )
        autovacuum_statements = [
            statement for statement, _ in statements if "autovacuum_enabled" in statement
        ]
        self.assertEqual(len(autovacuum_statements), 1)
        self.assertNotIn("ALTER DATABASE", sql.upper())
        self.assertTrue(self.connector.connections[0].application_name.startswith(DEMO_PREFIX))
        self.assertIn(
            "SELECT pg_catalog.current_database()",
            self.connector.connections[0].statements[0][0],
        )

    def test_live_effective_database_is_verified_before_any_demo_mutation(self) -> None:
        self.connector = _Connector(database_name="production")

        with self.assertRaises(DemoGuardError):
            self.runner().setup_bloat(rows=100)

        connection = self.connector.connections[0]
        self.assertTrue(connection.closed)
        self.assertEqual(
            connection.statements,
            [("SELECT pg_catalog.current_database()", None)],
        )

    def test_current_override_still_queries_effective_database_before_mutation(self) -> None:
        self.connector = _Connector(database_name="production")
        runner = DemoRunner(
            self.config,
            environ={"SCM_HEALING_ALLOW_DEMO": "1"},
            connect=self.connector,
            wait=lambda seconds: None,
        )

        runner.setup_bloat(rows=10)

        statements = self.connector.connections[0].statements
        self.assertEqual(statements[0], ("SELECT pg_catalog.current_database()", None))
        self.assertTrue(any("CREATE TABLE" in sql for sql, _ in statements[1:]))

    def test_bloat_rows_are_positive_and_bounded_before_connecting(self) -> None:
        runner = self.runner()
        for rows in (0, 1_000_001):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    runner.setup_bloat(rows=rows)
        self.assertEqual(self.connector.connections, [])

    def test_idle_hold_uses_tag_and_rolls_back_and_closes_on_interrupt(self) -> None:
        def interrupt(seconds: float | None) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.runner(wait=interrupt).hold_idle(seconds=None)
        connection = self.connector.connections[0]
        self.assertEqual(connection.application_name, "scm_ovs_demo_idle_transaction")
        self.assertFalse(connection.autocommit)
        self.assertTrue(any("BEGIN" in sql for sql, _ in connection.statements))
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_pressure_opens_exact_bounded_count_and_closes_every_connection(self) -> None:
        result = self.runner().hold_pressure(connections=3, seconds=0.1)
        self.assertEqual(result["connections_requested"], 3)
        self.assertEqual(len(self.connector.connections), 3)
        self.assertTrue(all(item.closed for item in self.connector.connections))
        self.assertTrue(
            all(item.application_name == "scm_ovs_demo_pressure" for item in self.connector.connections)
        )
        self.assertTrue(
            all(
                item.statements[0] == ("SELECT pg_catalog.current_database()", None)
                for item in self.connector.connections
            )
        )

    def test_pressure_partial_open_failure_closes_every_prior_connection(self) -> None:
        self.connector = _Connector(fail_on_call=3)

        with self.assertRaises(OSError):
            self.runner().hold_pressure(connections=4, seconds=1)

        self.assertEqual(len(self.connector.connections), 2)
        self.assertTrue(all(connection.closed for connection in self.connector.connections))

    def test_pressure_rejects_out_of_range_count_before_opening_connections(self) -> None:
        runner = self.runner()
        for count in (0, MAX_PRESSURE_CONNECTIONS + 1):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    runner.hold_pressure(connections=count, seconds=0)
        self.assertEqual(self.connector.connections, [])

    def test_cleanup_targets_only_tagged_sessions_and_exact_demo_table(self) -> None:
        result = self.runner().cleanup_all()
        statements = self.connector.connections[0].statements
        sql = "\n".join(statement for statement, _ in statements)
        parameters = [parameters for statement, parameters in statements if "pg_terminate_backend" in statement]
        self.assertEqual(result["status"], "cleaned")
        self.assertIn('DROP TABLE IF EXISTS "scm_ovs_demo_bloat"', sql)
        self.assertNotIn("DROP SCHEMA", sql.upper())
        self.assertNotIn("DROP DATABASE", sql.upper())
        escaped_prefix = DEMO_PREFIX.replace("_", r"\_")
        self.assertEqual(parameters, [(f"{escaped_prefix}%",)])
        self.assertIn("application_name LIKE", sql)
        self.assertIn("pid <> pg_catalog.pg_backend_pid()", sql)
        self.assertEqual(self.connector.connections[0].application_name, "scm_ovs_demo_cleanup")

    def test_every_created_identifier_and_application_tag_uses_demo_prefix(self) -> None:
        runner = self.runner()
        runner.setup_bloat(rows=10)
        runner.cleanup_all()
        for connection in self.connector.connections:
            self.assertTrue(connection.application_name.startswith(DEMO_PREFIX))
        all_sql = "\n".join(
            statement
            for connection in self.connector.connections
            for statement, _ in connection.statements
        )
        self.assertNotIn('CREATE TABLE "', all_sql)
        self.assertNotIn('DROP TABLE "', all_sql)
        self.assertIn('"scm_ovs_demo_bloat"', all_sql)


if __name__ == "__main__":
    unittest.main()
