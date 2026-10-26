"""Reading the Coordinator's report revisions from disk, and deriving what the UI shows from them.

Revisions are written by ``coordinator/revisions.py`` as ``<REPORTS_DIR>/<incident>/a<attempt>-r<N>.json``.
This module only reads them. ``complete`` and ``actionable`` are properties of the Coordinator's report
object, so they are not in the JSON; they are recomputed here with the same rules
(``tests/test_ui_reports.py`` pins them to ``coordinator.synthesis``).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from backend.config import settings

#: Same floor as ``coordinator.synthesis.MIN_REPORTABLE_CONFIDENCE`` and the Rego ``min_confidence``.
MIN_REPORTABLE_CONFIDENCE = 0.5
_NAME = re.compile(r"^a(?P<attempt>\d+)-r(?P<revision>\d+)\.json$")


def _directory(incident_id: UUID | str) -> Path:
    return Path(settings.REPORTS_DIR) / str(UUID(str(incident_id)))  # UUID(): no path traversal


def derive(report: dict[str, Any]) -> dict[str, Any]:
    """``complete`` / ``actionable`` / ``failed_agents`` exactly as the Coordinator defines them."""
    failed = [f["agent"] for f in report.get("findings", []) if f.get("status") == "failed"]
    outcomes = report.get("agent_outcomes") or {}
    complete = not report.get("missing_agents") and not failed and all(v == "completed" for v in outcomes.values())
    actionable = (
        report.get("status") == "resolved_cause"
        and report.get("failure_type") != "unknown"
        and float(report.get("confidence", 0)) >= MIN_REPORTABLE_CONFIDENCE
        and complete
    )
    return {"complete": complete, "actionable": actionable, "failed_agents": failed}


def revisions(incident_id: UUID | str) -> list[dict[str, Any]]:
    """Every stored revision, oldest first; unreadable files are skipped, never fatal."""
    directory = _directory(incident_id)
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in directory.iterdir():
        if not _NAME.match(path.name):
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            document["report"].update(derive(document["report"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found.append(document)
    return sorted(found, key=lambda d: (int(d.get("attempt", 0)), int(d.get("revision", 0))))


def latest(incident_id: UUID | str) -> dict[str, Any] | None:
    found = revisions(incident_id)
    return found[-1] if found else None


def summarize(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    """The few fields a list row needs."""
    if envelope is None:
        return None
    report = envelope["report"]
    conflicts = report.get("conflicts") or []
    return {
        "report_revision_id": envelope.get("report_revision_id"),
        "revision": envelope.get("revision"),
        "attempt": envelope.get("attempt"),
        "created_at": envelope.get("created_at"),
        "status": report.get("status"),
        "verdict": report.get("failure_type"),
        "confidence": report.get("confidence"),
        "complete": report["complete"],
        "actionable": report["actionable"],
        "conflicted": report.get("status") == "conflicting" or bool(conflicts),
        "summary": report.get("summary", ""),
        "missing_agents": report.get("missing_agents", []),
        "agent_outcomes": report.get("agent_outcomes", {}),
    }


def render_markdown(envelope: dict[str, Any]) -> str:
    report = envelope["report"]
    lines = [
        f"# Incident {report.get('incident_id')}",
        "",
        f"**Cause:** {report.get('failure_type')}  ",
        f"**Confidence:** {float(report.get('confidence', 0)):.2f}  ",
        f"**Status:** {report.get('status')}  ",
        f"**Complete:** {'yes' if report['complete'] else 'no'}  ",
        f"**Actionable:** {'yes' if report['actionable'] else 'no'}  ",
        f"**Revision:** {envelope.get('revision')} (attempt {envelope.get('attempt')})",
        "",
        report.get("summary", ""),
        "",
        "## What each agent found",
        "",
        "| Agent | Verdict | Confidence | Status |",
        "| --- | --- | --- | --- |",
    ]
    for finding in report.get("findings", []):
        lines.append(f"| {finding['agent']} | {finding['failure_type']} | {float(finding['confidence']):.2f} | {finding['status']} |")
    if report.get("conflicts"):
        lines += ["", "## Unresolved disagreement", ""]
        lines += [f"- {', '.join(c.get('claimed_by', []))} say {c.get('failure_type')} ({float(c.get('confidence', 0)):.2f})" for c in report["conflicts"]]
    if report.get("recommended_next_steps"):
        lines += ["", "## Recommended next steps", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(report["recommended_next_steps"], start=1)]
    return "\n".join(lines) + "\n"


def trim_evidence(payload: dict[str, Any], *, max_chars: int = 1500) -> dict[str, Any]:
    """An agent's evidence document, reduced to what the UI shows; long item bodies are cut."""
    items = []
    for item in payload.get("evidence_items", [])[:200]:
        content = str(item.get("content", ""))
        items.append(
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "source": item.get("source"),
                "content": content[:max_chars] + ("…" if len(content) > max_chars else ""),
            }
        )
    return {
        "agent": payload.get("agent"),
        "status": payload.get("status"),
        "failure_type": payload.get("failure_type"),
        "confidence": payload.get("confidence"),
        "summary": payload.get("summary"),
        "created_at": payload.get("created_at"),
        "investigation_coverage": payload.get("investigation_coverage"),
        "coverage_notes": payload.get("coverage_notes", []),
        "recommended_next_steps": payload.get("recommended_next_steps", []),
        "hypotheses": payload.get("root_cause_hypotheses", []),
        "evidence_items": items,
    }
