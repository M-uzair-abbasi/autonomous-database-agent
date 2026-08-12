"""Operator CLI contracts that must remain safe without PostgreSQL installed."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scm_agent.healing import cli
from scm_agent.healing.engine import CycleSummary
from scm_agent.healing.models import Mode


SECRET_DSN = "postgresql://operator:audit-sentinel-password@db.invalid/production"


class _Audit:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_targets(self) -> list[dict[str, object]]:
        return [{"id": 7, "label": "target-safe"}]

    def list_events(self, target_id: int) -> list[dict[str, object]]:
        return [{"id": 11, "target_key": "safe-table", "state": "detected"}]

    def list_actions(self, target_id: int) -> list[dict[str, object]]:
        return [{"id": 13, "action_kind": "vacuum", "state": "simulated"}]


class _Gateway:
    def __init__(self, config: object) -> None:
        self.config = config

    def doctor(self) -> dict[str, object]:
        return {
            "driver_available": False,
            "database_reachable": False,
            "notes": ["Install the PostgreSQL driver in the project virtual environment"],
        }


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str], **dependencies: object) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        environ = {
            "SCM_DB_DSN": SECRET_DSN,
            "SCM_HEALING_AUDIT_PATH": str(Path(self.tempdir.name) / "audit.sqlite3"),
        }
        environ.update(dependencies.pop("environ", {}))  # type: ignore[arg-type]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv, environ=environ, **dependencies)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SECRET_DSN, combined)
        self.assertNotIn("audit-sentinel-password", combined)
        return code, stdout.getvalue(), stderr.getvalue()

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_module_help_is_driverless_and_has_no_dsn_argument(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "scm_agent.healing", "--help"],
            cwd=Path(__file__).resolve().parents[3],
            env={"PATH": "", "PYTHONPATH": str(Path(__file__).resolve().parents[3])},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("doctor", result.stdout)
        self.assertIn("reindex", result.stdout)
        self.assertNotIn("--dsn", result.stdout + result.stderr)

    def test_unknown_dsn_argument_does_not_echo_its_secret_value(self) -> None:
        code, _stdout, stderr = self.run_cli(
            ["once", "--dsn", SECRET_DSN],
            audit_factory=_Audit,
            gateway_factory=_Gateway,
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error"], "invalid_arguments")

    def test_once_defaults_to_observe_and_prints_a_secret_free_json_summary(self) -> None:
        constructed: list[Mode] = []

        class Engine:
            def __init__(self, config: object, gateway: object, audit: object) -> None:
                constructed.append(config.mode)  # type: ignore[attr-defined]

            def run_once(self) -> CycleSummary:
                return CycleSummary(status="completed", finding_count=2)

        code, stdout, stderr = self.run_cli(
            ["once"],
            audit_factory=_Audit,
            gateway_factory=_Gateway,
            engine_factory=Engine,
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(constructed, [Mode.OBSERVE])
        self.assertEqual(
            json.loads(stdout),
            {"command": "once", "result": asdict(CycleSummary(status="completed", finding_count=2))},
        )

    def test_events_and_actions_read_only_sqlite_without_constructing_gateway(self) -> None:
        def forbidden_gateway(config: object) -> object:
            raise AssertionError("listing commands must not construct PostgreSQL gateway")

        for command, expected_key in (("events", "target_key"), ("actions", "action_kind")):
            with self.subTest(command=command):
                code, stdout, stderr = self.run_cli(
                    [command],
                    audit_factory=_Audit,
                    gateway_factory=forbidden_gateway,
                )
                self.assertEqual((code, stderr), (0, ""))
                payload = json.loads(stdout)
                self.assertEqual(payload["command"], command)
                self.assertEqual(payload["targets"][0]["target_id"], 7)
                self.assertIn(expected_key, payload["targets"][0]["items"][0])

    def test_invalid_mode_is_a_sanitized_usage_error(self) -> None:
        code, stdout, stderr = self.run_cli(
            ["once", "--mode", "audit-sentinel-password"],
            audit_factory=_Audit,
            gateway_factory=_Gateway,
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr), {"command": "cli", "error": "invalid_arguments"})

    def test_doctor_checks_audit_before_reporting_driver_unavailable(self) -> None:
        calls: list[str] = []

        class Audit(_Audit):
            def __init__(self, path: Path) -> None:
                calls.append("audit")
                super().__init__(path)

        class Gateway(_Gateway):
            def __init__(self, config: object) -> None:
                calls.append("gateway")
                super().__init__(config)

            def doctor(self) -> dict[str, object]:
                calls.append("database")
                return super().doctor()

        code, stdout, stderr = self.run_cli(
            ["doctor"], audit_factory=Audit, gateway_factory=Gateway
        )
        self.assertEqual((code, stderr), (3, ""))
        self.assertEqual(calls, ["audit", "gateway", "database"])
        payload = json.loads(stdout)
        self.assertTrue(payload["audit"]["writable"])
        self.assertFalse(payload["database"]["driver_available"])

    def test_doctor_stops_before_database_when_audit_is_not_writable(self) -> None:
        def broken_audit(path: Path) -> object:
            raise OSError(f"cannot write {SECRET_DSN}")

        def forbidden_gateway(config: object) -> object:
            raise AssertionError("doctor must fail closed before database capability checks")

        code, stdout, stderr = self.run_cli(
            ["doctor"], audit_factory=broken_audit, gateway_factory=forbidden_gateway
        )
        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(
            json.loads(stdout),
            {"audit": {"writable": False}, "command": "doctor", "status": "failed"},
        )

    def test_act_reindex_requires_confirmation_and_current_environment_flag(self) -> None:
        base = ["reindex", "public", "orders_customer_idx", "--mode", "act"]
        cases = (
            (base + ["--confirm"], {}, "manual_reindex_disabled"),
            (base, {"SCM_HEALING_ALLOW_MANUAL_REINDEX": "1"}, "confirmation_required"),
        )
        for argv, extra_env, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                code, stdout, stderr = self.run_cli(
                    argv,
                    environ=extra_env,
                    audit_factory=_Audit,
                    gateway_factory=_Gateway,
                )
                self.assertEqual((code, stdout), (2, ""))
                self.assertEqual(json.loads(stderr)["error"], expected_error)


if __name__ == "__main__":
    unittest.main()
