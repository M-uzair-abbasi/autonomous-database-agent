"""Operator CLI contracts that must remain safe without PostgreSQL installed."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scm_agent.healing import cli
from scm_agent.healing.actions import ActionPlanner
from scm_agent.healing.audit import SQLiteAuditRepository
from scm_agent.healing.config import HealingConfig
from scm_agent.healing.engine import CycleSummary
from scm_agent.healing.models import (
    ActionResult,
    ActionStatus,
    Finding,
    Mode,
    RuleId,
    Severity,
    TargetKind,
)
from scm_agent.healing.postgres import CapabilityDecision


SECRET_DSN = "postgresql://operator:audit-sentinel-password@db.invalid/production"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


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

    def test_run_keyboard_interrupt_returns_structured_exit_130(self) -> None:
        class Engine:
            def __init__(self, config: object, gateway: object, audit: object) -> None:
                pass

            def run_forever(self, stop: object) -> None:
                raise KeyboardInterrupt

        code, stdout, stderr = self.run_cli(
            ["run"],
            audit_factory=_Audit,
            gateway_factory=_Gateway,
            engine_factory=Engine,
        )
        self.assertEqual((code, stderr), (130, ""))
        self.assertEqual(json.loads(stdout), {"command": "run", "status": "interrupted"})

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

    def test_reindex_starts_cycle_before_capability_and_commits_before_execute(self) -> None:
        audit_path = Path(self.tempdir.name) / "manual.sqlite3"
        repository = SQLiteAuditRepository(audit_path)

        class Gateway:
            execute_count = 0
            saw_started_cycle = False
            saw_committed_intent = False

            def __init__(self, config: HealingConfig) -> None:
                self.config = config

            def check_plan(self, plan: object) -> CapabilityDecision:
                with repository._connect() as connection:
                    self.saw_started_cycle = connection.execute(
                        "SELECT COUNT(*) FROM poll_cycle"
                    ).fetchone()[0] == 1
                return CapabilityDecision(True, "ok")

            def capture_before(self, plan: object) -> dict[str, object]:
                return {"present": True}

            def execute(self, plan: object) -> ActionResult:
                self.execute_count += 1
                actions = repository.list_actions(repository.list_targets()[0]["id"])
                self.saw_committed_intent = actions[-1]["state"] == "executing"
                return ActionResult(
                    plan.action_kind,  # type: ignore[attr-defined]
                    ActionStatus.SUCCEEDED,
                    plan.target_key,  # type: ignore[attr-defined]
                    NOW,
                    NOW,
                )

            def capture_after(self, plan: object) -> dict[str, object]:
                return {"present": True}

        gateway = Gateway(
            HealingConfig(
                database_dsn=SECRET_DSN,
                mode=Mode.ACT,
                audit_path=audit_path,
                allow_manual_reindex=True,
            )
        )
        code, stdout, stderr = self.run_cli(
            ["reindex", "public", "orders_customer_idx", "--mode", "act", "--confirm"],
            environ={
                "SCM_HEALING_ALLOW_MANUAL_REINDEX": "1",
                "SCM_HEALING_AUDIT_PATH": str(audit_path),
            },
            audit_factory=lambda path: repository,
            gateway_factory=lambda config: gateway,
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "succeeded")
        self.assertTrue(gateway.saw_started_cycle)
        self.assertTrue(gateway.saw_committed_intent)
        self.assertEqual(gateway.execute_count, 1)

    def test_later_reindex_run_reconciles_orphan_before_database_io(self) -> None:
        audit_path = Path(self.tempdir.name) / "reconcile.sqlite3"
        repository = SQLiteAuditRepository(audit_path)
        config = HealingConfig(
            database_dsn=SECRET_DSN,
            mode=Mode.ACT,
            audit_path=audit_path,
            allow_manual_reindex=True,
        )
        plan = ActionPlanner().plan_manual_reindex(
            "public", "orders_customer_idx", config, confirmed=True
        )
        target_id = repository.register_target(config.database_dsn, "fixture")
        unrelated_cycle = repository.start_cycle(target_id, Mode.OBSERVE)
        unrelated_event_id = repository.record_finding(
            unrelated_cycle,
            Finding(
                rule_id=RuleId.UNUSED_LARGE_INDEX,
                severity=Severity.WARNING,
                target_kind=TargetKind.INDEX,
                target_key="unrelated-index-identity",
                evidence={"index_name": "unrelated_idx"},
                recommendation="review separately",
                action_eligible=False,
                observed_at=NOW,
                fingerprint="unused-index-unrelated-cli-fixture",
            ),
            "indexes",
        )
        repository.finish_cycle(unrelated_cycle, {"indexes": True})
        cycle_id = repository.start_cycle(target_id, config.mode)
        event_id = repository.record_finding(
            cycle_id,
            Finding(
                rule_id=RuleId.UNUSED_LARGE_INDEX,
                severity=Severity.WARNING,
                target_kind=TargetKind.INDEX,
                target_key=plan.target_key,
                evidence={"manual": True},
                recommendation="manual",
                action_eligible=False,
                observed_at=NOW,
                fingerprint=plan.finding_fingerprint,
            ),
            "indexes",
        )
        orphan = repository.acquire_action_intent(
            event_id, plan, "manual-orphan-fixture", None
        )

        def forbidden_gateway(config: HealingConfig) -> object:
            raise AssertionError("reconciliation must occur before PostgreSQL adapter construction")

        code, stdout, stderr = self.run_cli(
            ["reindex", "public", "orders_customer_idx", "--mode", "act", "--confirm"],
            environ={
                "SCM_HEALING_ALLOW_MANUAL_REINDEX": "1",
                "SCM_HEALING_AUDIT_PATH": str(audit_path),
            },
            audit_factory=lambda path: repository,
            gateway_factory=forbidden_gateway,
        )

        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(json.loads(stdout)["status"], "recovered_orphan")
        self.assertEqual(repository.list_actions(target_id)[0]["state"], "outcome_unknown")
        self.assertEqual(orphan.action_id, repository.list_actions(target_id)[0]["id"])
        unrelated = next(
            item for item in repository.list_events(target_id) if item["id"] == unrelated_event_id
        )
        self.assertEqual(unrelated["state"], "detected")

    def test_reindex_capability_exception_is_terminal_and_secret_free(self) -> None:
        audit_path = Path(self.tempdir.name) / "capability.sqlite3"
        repository = SQLiteAuditRepository(audit_path)

        class Gateway:
            def check_plan(self, plan: object) -> CapabilityDecision:
                raise OSError(SECRET_DSN)

        code, stdout, stderr = self.run_cli(
            ["reindex", "public", "orders_customer_idx", "--mode", "act", "--confirm"],
            environ={
                "SCM_HEALING_ALLOW_MANUAL_REINDEX": "1",
                "SCM_HEALING_AUDIT_PATH": str(audit_path),
            },
            audit_factory=lambda path: repository,
            gateway_factory=lambda config: Gateway(),
        )

        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(json.loads(stdout), {"command": "reindex", "status": "failed"})
        with repository._connect() as connection:
            states = [
                row[0]
                for row in connection.execute(
                    "SELECT state FROM cycle_transition ORDER BY id"
                )
            ]
        self.assertEqual(states, ["started", "failed"])

    def test_post_cycle_start_interrupts_terminalize_then_return_130(self) -> None:
        for stage in ("reconcile", "finding", "gateway", "recovered_finish"):
            with self.subTest(stage=stage):
                audit_path = Path(self.tempdir.name) / f"interrupt-{stage}.sqlite3"

                class Repository(SQLiteAuditRepository):
                    def reconcile_orphans(self) -> list[int]:
                        if stage == "reconcile":
                            raise KeyboardInterrupt
                        if stage == "recovered_finish":
                            return [991]
                        return super().reconcile_orphans()

                    def record_finding(self, *args: object, **kwargs: object) -> int:
                        if stage == "finding":
                            raise KeyboardInterrupt
                        return super().record_finding(*args, **kwargs)

                    def finish_cycle(
                        self, cycle_id: int, collector_statuses: dict[str, bool]
                    ) -> int:
                        if stage == "recovered_finish":
                            raise KeyboardInterrupt
                        return super().finish_cycle(cycle_id, collector_statuses)

                repository = Repository(audit_path)

                def gateway_factory(config: HealingConfig) -> object:
                    if stage == "gateway":
                        raise KeyboardInterrupt
                    return _Gateway(config)

                code, stdout, stderr = self.run_cli(
                    ["reindex", "public", "orders_customer_idx", "--mode", "act", "--confirm"],
                    environ={
                        "SCM_HEALING_ALLOW_MANUAL_REINDEX": "1",
                        "SCM_HEALING_AUDIT_PATH": str(audit_path),
                    },
                    audit_factory=lambda path: repository,
                    gateway_factory=gateway_factory,
                )

                self.assertEqual((code, stderr), (130, ""))
                self.assertEqual(json.loads(stdout), {"command": "reindex", "status": "interrupted"})
                with repository._connect() as connection:
                    states = [
                        row[0]
                        for row in connection.execute(
                            "SELECT state FROM cycle_transition ORDER BY id"
                        )
                    ]
                self.assertEqual(states, ["started", "failed"])


if __name__ == "__main__":
    unittest.main()
