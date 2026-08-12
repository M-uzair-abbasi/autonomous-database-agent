"""Secret-safe operator CLI for the SCM-OVS healing engine."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Callable, Mapping, Sequence

from .config import HealingConfig
from .models import (
    ActionResult,
    ActionStatus,
    Finding,
    Mode,
    RuleId,
    Severity,
    TargetKind,
)


EXIT_OK = 0
EXIT_OPERATION_FAILED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_INTERRUPTED = 130


class _SafeArgumentError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    """Avoid argparse echoing an unknown argument that may contain a secret."""

    def error(self, message: str) -> None:
        raise _SafeArgumentError("invalid_arguments")


def _add_mode(parser: argparse.ArgumentParser, *, default: str = Mode.OBSERVE.value) -> None:
    parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in Mode),
        default=default,
        help="operating mode (default: %(default)s)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="python3 -m scm_agent.healing",
        description="Audit-first PostgreSQL health monitoring and guarded healing.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="check the local audit journal, driver, and database capabilities")

    once = commands.add_parser("once", help="run one collection and healing cycle")
    _add_mode(once)

    run = commands.add_parser("run", help="run serial cycles until interrupted")
    _add_mode(run)

    commands.add_parser("events", help="list anomaly events from the local SQLite audit journal")
    commands.add_parser("actions", help="list action attempts from the local SQLite audit journal")

    reindex = commands.add_parser("reindex", help="request one guarded concurrent index rebuild")
    reindex.add_argument("schema", help="exact user schema name")
    reindex.add_argument("index", help="exact index name")
    _add_mode(reindex, default=Mode.DRY_RUN.value)
    reindex.add_argument(
        "--confirm",
        action="store_true",
        help="required with act mode in addition to SCM_HEALING_ALLOW_MANUAL_REINDEX=1",
    )

    demo = commands.add_parser("demo", help="run explicitly guarded disposable demo scenarios")
    scenarios = demo.add_subparsers(dest="scenario", required=True)
    bloat = scenarios.add_parser("bloat", help="create or remove the disposable bloat table")
    bloat.add_argument("operation", choices=("setup", "cleanup"))
    bloat.add_argument("--rows", type=int, default=20_000)
    idle = scenarios.add_parser("idle", help="hold or clean up a tagged idle transaction")
    idle.add_argument("operation", choices=("hold", "cleanup"))
    idle.add_argument("--seconds", type=float)
    pressure = scenarios.add_parser("pressure", help="hold or clean up tagged demo connections")
    pressure.add_argument("operation", choices=("hold", "cleanup"))
    pressure.add_argument("--connections", type=int, default=10)
    pressure.add_argument("--seconds", type=float)
    cleanup = scenarios.add_parser("cleanup", help="remove every tagged demo resource")
    cleanup.set_defaults(operation="cleanup")
    return parser


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, Path)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    destination = sys.stderr if error else sys.stdout
    print(json.dumps(payload, default=_json_default, sort_keys=True), file=destination)


def _configuration(arguments: argparse.Namespace, environ: Mapping[str, str]) -> HealingConfig:
    values = dict(environ)
    if hasattr(arguments, "mode"):
        values["SCM_HEALING_MODE"] = arguments.mode
    return HealingConfig.from_env(values)


def _audit_factory_default(path: Path) -> object:
    from .audit import SQLiteAuditRepository

    return SQLiteAuditRepository(path)


def _gateway_factory_default(config: HealingConfig) -> object:
    from .postgres import PostgresGateway

    return PostgresGateway(config)


def _engine_factory_default(config: HealingConfig, gateway: object, audit: object) -> object:
    from .engine import HealingEngine

    return HealingEngine(config, gateway, audit)  # type: ignore[arg-type]


def _history(command: str, audit: object) -> dict[str, object]:
    listed: list[dict[str, object]] = []
    method_name = "list_events" if command == "events" else "list_actions"
    for target in audit.list_targets():  # type: ignore[attr-defined]
        target_id = int(target["id"])
        items = getattr(audit, method_name)(target_id)
        listed.append({"target_id": target_id, "items": items})
    return {"command": command, "targets": listed}


def _manual_reindex(
    arguments: argparse.Namespace,
    config: HealingConfig,
    audit: object,
    gateway: object,
) -> tuple[int, dict[str, object]]:
    from .actions import ActionPlanner

    plan = ActionPlanner().plan_manual_reindex(
        arguments.schema,
        arguments.index,
        config,
        confirmed=arguments.confirm,
    )
    decision = gateway.check_plan(plan)  # type: ignore[attr-defined]
    if not decision.allowed:
        return EXIT_UNAVAILABLE, {
            "command": "reindex",
            "status": "rejected",
            "reason": decision.reason,
        }

    target_id = audit.register_target(config.database_dsn, "scm-ovs-target")  # type: ignore[attr-defined]
    cycle_id = audit.start_cycle(target_id, config.mode)  # type: ignore[attr-defined]
    finding = Finding(
        rule_id=RuleId.UNUSED_LARGE_INDEX,
        severity=Severity.WARNING,
        target_kind=TargetKind.INDEX,
        target_key=plan.target_key,
        evidence={"schema_name": arguments.schema, "index_name": arguments.index, "manual": True},
        recommendation="operator requested concurrent reindex",
        action_eligible=False,
        observed_at=datetime.now(timezone.utc),
        fingerprint=plan.finding_fingerprint,
    )
    event_id = audit.record_finding(cycle_id, finding, "indexes")  # type: ignore[attr-defined]
    key_source = f"{cycle_id}\x00{plan.finding_fingerprint}\x00{config.mode.value}"
    acquisition = audit.acquire_action_intent(  # type: ignore[attr-defined]
        event_id,
        plan,
        f"manualreindex-{sha256(key_source.encode('utf-8')).hexdigest()}",
        None,
    )
    if acquisition.status != "acquired" or acquisition.action_id is None:
        audit.finish_cycle(cycle_id, {"indexes": True})  # type: ignore[attr-defined]
        return EXIT_OPERATION_FAILED, {"command": "reindex", "status": acquisition.status}

    action_id = acquisition.action_id
    audit.record_snapshot(action_id, "before", gateway.capture_before(plan))  # type: ignore[attr-defined]
    if config.mode is Mode.DRY_RUN:
        now = datetime.now(timezone.utc)
        result = ActionResult(
            plan.action_kind,
            ActionStatus.SIMULATED,
            plan.target_key,
            now,
            now,
            evidence={"reason": "dry_run"},
        )
    else:
        now = datetime.now(timezone.utc)
        audit.record_action_result(  # type: ignore[attr-defined]
            action_id,
            ActionResult(plan.action_kind, ActionStatus.EXECUTING, plan.target_key, now),
        )
        result = gateway.execute(plan)  # type: ignore[attr-defined]
    audit.record_snapshot(action_id, "after", gateway.capture_after(plan))  # type: ignore[attr-defined]
    audit.record_action_result(action_id, result)  # type: ignore[attr-defined]
    audit.finish_cycle(cycle_id, {"indexes": True})  # type: ignore[attr-defined]
    success = result.status in (ActionStatus.SIMULATED, ActionStatus.SUCCEEDED)
    return (EXIT_OK if success else EXIT_OPERATION_FAILED), {
        "command": "reindex",
        "status": result.status.value,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    audit_factory: Callable[[Path], object] | None = None,
    gateway_factory: Callable[[HealingConfig], object] | None = None,
    engine_factory: Callable[[HealingConfig, object, object], object] | None = None,
    demo_runner_factory: Callable[[HealingConfig, Mapping[str, str]], object] | None = None,
) -> int:
    """Dispatch one CLI invocation and return a process-compatible exit code."""
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except _SafeArgumentError:
        _emit({"command": "cli", "error": "invalid_arguments"}, error=True)
        return EXIT_USAGE

    values = dict(os.environ if environ is None else environ)
    try:
        config = _configuration(arguments, values)
    except (TypeError, ValueError):
        _emit({"command": arguments.command, "error": "invalid_configuration"}, error=True)
        return EXIT_USAGE

    make_audit = audit_factory or _audit_factory_default
    make_gateway = gateway_factory or _gateway_factory_default
    make_engine = engine_factory or _engine_factory_default

    if arguments.command == "reindex" and config.mode is Mode.ACT:
        if not config.allow_manual_reindex:
            _emit({"command": "reindex", "error": "manual_reindex_disabled"}, error=True)
            return EXIT_USAGE
        if not arguments.confirm:
            _emit({"command": "reindex", "error": "confirmation_required"}, error=True)
            return EXIT_USAGE

    try:
        if arguments.command in ("events", "actions"):
            audit = make_audit(config.audit_path)
            _emit(_history(arguments.command, audit))
            return EXIT_OK

        if arguments.command == "doctor":
            try:
                make_audit(config.audit_path)
            except Exception:
                _emit({"audit": {"writable": False}, "command": "doctor", "status": "failed"})
                return EXIT_OPERATION_FAILED
            gateway = make_gateway(config)
            database = dict(gateway.doctor())  # type: ignore[attr-defined]
            available = bool(database.get("driver_available") and database.get("database_reachable"))
            _emit({
                "audit": {"writable": True},
                "command": "doctor",
                "database": database,
                "status": "ok" if available else "unavailable",
            })
            return EXIT_OK if available else EXIT_UNAVAILABLE

        if arguments.command in ("once", "run"):
            audit = make_audit(config.audit_path)
            gateway = make_gateway(config)
            engine = make_engine(config, gateway, audit)
            if arguments.command == "once":
                result = engine.run_once()  # type: ignore[attr-defined]
                _emit({"command": "once", "result": result})
                return EXIT_OK if result.status == "completed" else EXIT_OPERATION_FAILED
            engine.run_forever(lambda: False)  # type: ignore[attr-defined]
            _emit({"command": "run", "status": "stopped"})
            return EXIT_OK

        if arguments.command == "reindex":
            audit = make_audit(config.audit_path)
            gateway = make_gateway(config)
            code, payload = _manual_reindex(arguments, config, audit, gateway)
            _emit(payload)
            return code

        if arguments.command == "demo":
            if demo_runner_factory is None:
                from .demo import DemoRunner

                runner = DemoRunner(config, environ=values)
            else:
                runner = demo_runner_factory(config, values)
            payload = runner.dispatch(  # type: ignore[attr-defined]
                arguments.scenario,
                arguments.operation,
                rows=getattr(arguments, "rows", None),
                connections=getattr(arguments, "connections", None),
                seconds=getattr(arguments, "seconds", None),
            )
            _emit({"command": "demo", "result": payload})
            return EXIT_OK
    except KeyboardInterrupt:
        _emit({"command": arguments.command, "status": "interrupted"})
        return EXIT_INTERRUPTED
    except Exception:
        _emit({"command": arguments.command, "error": "operation_failed"}, error=True)
        return EXIT_OPERATION_FAILED

    _emit({"command": "cli", "error": "invalid_arguments"}, error=True)
    return EXIT_USAGE


__all__ = ["build_parser", "main"]
