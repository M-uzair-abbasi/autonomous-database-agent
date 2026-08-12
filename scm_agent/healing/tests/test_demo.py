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
        return (4321,)

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Connection:
    def __init__(self, application_name: str, autocommit: bool) -> None:
        self.application_name = application_name
        self.autocommit = autocommit
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
    def __init__(self) -> None:
        self.connections: list[_Connection] = []

    def __call__(self, dsn: str, **kwargs: object) -> _Connection:
        connection = _Connection(
            str(kwargs["application_name"]), bool(kwargs["autocommit"])
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
