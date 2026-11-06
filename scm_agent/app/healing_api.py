"""Read-only HTTP view of the healing audit journal.

The healing engine records every anomaly it detects and every action it takes
into a local SQLite journal. Until now that journal was reachable only through
the CLI, so the dashboard had nothing to render on its audit screen.

This module is read-only by construction: it opens the journal, selects, and
closes. It cannot start a healing cycle, plan an action, or execute one — those
stay behind the CLI where they require an explicit mode and confirmation. A
dashboard that could trigger a `VACUUM` from a button click is a different and
much more dangerous product than one that shows you what already happened.

Framework-free, like the rest of `app/`: every function takes ordinary
arguments and returns an `(int, dict)` pair, so the audit view can be tested
without starting a server.

Absence is a normal state, not an error. Before the engine has ever run there
is no journal file at all, and the honest answer to "what has healing done?" is
"nothing yet" — a 200 with `available: false`, not a 500.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HTTP_OK = 200
HTTP_SERVER_ERROR = 500

DEFAULT_JOURNAL = Path("var/scm_ovs/healing-audit.sqlite3")
"""Matches `HealingConfig.audit_path`. Kept as a module constant rather than
imported from the healing package so the API layer does not depend on Pillar 2
being importable — the query dashboard must work even if healing is absent."""

MAX_ROWS = 200
"""Upper bound on events and actions returned. The journal is append-only and
grows without limit; a dashboard does not need all of it, and an unbounded
response is a denial of service with extra steps."""


def _parse_detail(value: Any) -> Any:
    """Decode a `*_json` column, tolerating nulls and malformed rows.

    Journal rows are written by another subsystem. If one is unreadable the
    audit view should degrade to showing the row without its detail, not fail
    the whole request.
    """
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {"unparsed": str(value)[:200]}


def _clean_transitions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "state": row.get("state"),
            "at": row.get("occurred_at"),
            "error": row.get("error"),
            "detail": _parse_detail(row.get("detail_json")),
        }
        for row in rows
    ]


def read_audit(
    journal_path: Path | None = None, limit: int = MAX_ROWS
) -> tuple[int, dict[str, Any]]:
    """Return the healing journal contents as a JSON-able payload.

    Returns `(status_code, payload)`. `available` is False, with a 200, when no
    journal exists yet — that is the state before healing has ever been run, and
    the dashboard should say so plainly rather than showing an error.
    """
    path = Path(journal_path) if journal_path is not None else DEFAULT_JOURNAL
    limit = max(1, min(int(limit), MAX_ROWS))

    if not path.exists():
        return HTTP_OK, {
            "available": False,
            "reason": (
                "No healing journal yet. The engine writes one the first time "
                "it runs: python3 -m scm_agent.healing once"
            ),
            "journal": str(path),
            "targets": [],
            "events": [],
            "actions": [],
            "counts": {"targets": 0, "events": 0, "actions": 0},
        }

    try:
        # Imported here, not at module scope: the healing package pulls in its
        # own config and models, and the query dashboard must keep working if
        # Pillar 2 is missing or broken.
        from scm_agent.healing.audit import SQLiteAuditRepository

        repository = SQLiteAuditRepository(path)
        targets = repository.list_targets()

        events: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        for target in targets:
            target_id = int(target["id"])
            for event in repository.list_events(target_id):
                events.append(
                    {
                        "id": event.get("id"),
                        "rule": event.get("rule_id"),
                        "severity": event.get("severity"),
                        "target_kind": event.get("target_kind"),
                        "target_key": event.get("target_key"),
                        "state": event.get("state"),
                        "transitions": _clean_transitions(event.get("transitions") or []),
                    }
                )
            for action in repository.list_actions(target_id):
                actions.append(
                    {
                        "id": action.get("id"),
                        "action": action.get("action_kind"),
                        "target_key": action.get("target_key"),
                        "mode": action.get("mode"),
                        "command": action.get("command"),
                        "state": action.get("state"),
                        "transitions": _clean_transitions(action.get("transitions") or []),
                        "snapshots": len(action.get("snapshots") or []),
                    }
                )
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a 500
        return HTTP_SERVER_ERROR, {
            "available": False,
            "reason": f"could not read the healing journal: {type(exc).__name__}",
            "journal": str(path),
            "targets": [],
            "events": [],
            "actions": [],
            "counts": {"targets": 0, "events": 0, "actions": 0},
        }

    # Newest first: an operator opening the audit screen wants the most recent
    # activity, not the first thing that ever happened.
    events = sorted(events, key=lambda e: e["id"] or 0, reverse=True)[:limit]
    actions = sorted(actions, key=lambda a: a["id"] or 0, reverse=True)[:limit]

    return HTTP_OK, {
        "available": True,
        "journal": str(path),
        # `target_hash` is a SHA-256 of the DSN. The DSN itself is never stored
        # in the journal, and the hash must not be exposed here either.
        "targets": [
            {
                "id": t.get("id"),
                "label": t.get("label"),
                "registered_at": t.get("registered_at"),
            }
            for t in targets
        ],
        "events": events,
        "actions": actions,
        "counts": {
            "targets": len(targets),
            "events": len(events),
            "actions": len(actions),
        },
    }


if __name__ == "__main__":
    import tempfile

    print("=" * 70)
    print("HEALING AUDIT API")
    print("=" * 70)

    missing = Path(tempfile.gettempdir()) / "scm-ovs-no-such-journal.sqlite3"
    status, payload = read_audit(missing)
    print(f"\n  no journal yet -> HTTP {status}, available={payload['available']}")
    print(f"    reason: {payload['reason']}")
    assert status == HTTP_OK, "absence is not an error"
    assert payload["available"] is False
    assert payload["events"] == [] and payload["actions"] == []

    with tempfile.TemporaryDirectory() as tmp:
        journal = Path(tmp) / "audit.sqlite3"
        from scm_agent.healing.audit import SQLiteAuditRepository

        SQLiteAuditRepository(journal)  # creates the schema
        status, payload = read_audit(journal)
        print(f"\n  empty journal -> HTTP {status}, available={payload['available']}")
        print(f"    counts: {payload['counts']}")
        assert status == HTTP_OK
        assert payload["available"] is True

        blob = json.dumps(payload)
        assert "target_hash" not in blob, "the DSN hash must not be exposed"
        print("    target_hash absent from payload: True")

    _, clamped = read_audit(missing, limit=10_000)
    print("\n  oversized limit clamped without error: True")

    print("\n  all checks passed")
