"""Per-project numbers for the charts, computed from ``incidents`` and ``audit_events``.

Aggregation happens in Python on purpose: it behaves the same on PostgreSQL and SQLite (so it is tested
without a server) and a project's rows are few. The definitions match the Agent Ops Grafana dashboard:
"fix success" (the accuracy proxy) is the share of re-validated fixes that passed, not verdict accuracy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.audit_models import Approval, AuditEvent, ProposalStatus, RemediationProposal
from backend.db.database import get_db
from backend.db.models import Incident
from backend.db.ui_models import User
from backend.ui.deps import current_user, project_for
from backend.ui.projects import incidents_of

router = APIRouter(tags=["ui-stats"])

WANTED = (
    "synthesis_completed",
    "agent_completed",
    "agent_dispatch",
    "approval_decided",
    "revalidation_completed",
    "policy_evaluated",
    "pull_request_created",
)
PROBLEM_OUTCOMES = {"failed", "timed_out", "not_dispatched"}
OUTCOMES_SQL = Path(__file__).resolve().parents[2] / "monitoring" / "sql" / "incident_outcomes.sql"
OUTCOME_ORDER = ("resolved_by_fix", "awaiting_validation", "awaiting_approval", "policy_blocked", "open", "rejected", "escalated")


def aware(moment: datetime | str) -> datetime:
    if isinstance(moment, str):  # SQLite hands timestamps back as text
        moment = datetime.fromisoformat(moment)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 2)


def _durations(events: list[AuditEvent]) -> dict[str, dict[str, Any]]:
    """Per agent: run durations in seconds and how many runs failed or timed out."""
    own_runs: dict[tuple[str, str], list[float]] = defaultdict(list)
    for event in events:
        if event.event_type == "agent_completed" and event.duration_ms is not None:
            own_runs[(event.incident_id, event.actor)].append(event.duration_ms / 1000)
    per_agent: dict[str, dict[str, Any]] = defaultdict(lambda: {"durations": [], "runs": 0, "problems": 0})
    for (_, agent), values in own_runs.items():
        per_agent[agent]["durations"] += values
        per_agent[agent]["runs"] += len(values)
    for event in events:
        if event.event_type != "agent_dispatch":
            continue
        agent = str((event.payload or {}).get("agent", ""))
        outcome = (event.payload or {}).get("outcome")
        if outcome in PROBLEM_OUTCOMES:
            per_agent[agent]["problems"] += 1
        if (event.incident_id, agent) in own_runs:
            continue  # the agent audited its own run; do not count the dispatch twice
        per_agent[agent]["runs"] += 1
        if event.duration_ms is not None and outcome == "completed":
            per_agent[agent]["durations"].append(event.duration_ms / 1000)
    return {
        agent: {"runs": data["runs"], "problems": data["problems"], "p50_s": percentile(data["durations"], 0.5), "p95_s": percentile(data["durations"], 0.95)}
        for agent, data in sorted(per_agent.items())
        if agent
    }


async def outcomes(db: AsyncSession, ids: list[str]) -> dict[str, Any]:
    """How each incident ended (or where it is), from the same ``incident_outcomes`` view the dashboards use."""
    if not ids:
        return {"by_outcome": [], "resolved_p50_s": None, "resolved_p95_s": None}
    view = "\n".join(line for line in OUTCOMES_SQL.read_text(encoding="utf-8").splitlines() if not line.startswith("--"))
    query = text(f"WITH incident_outcomes AS ({view}) SELECT outcome, opened_at, closed_at FROM incident_outcomes WHERE incident_id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    rows = (await db.execute(query, {"ids": ids})).all()
    counts = Counter(row.outcome for row in rows)
    durations = [(aware(row.closed_at) - aware(row.opened_at)).total_seconds() for row in rows if row.outcome == "resolved_by_fix" and row.closed_at]
    return {
        "by_outcome": [{"outcome": name, "count": counts[name]} for name in OUTCOME_ORDER if counts[name]],
        "resolved_p50_s": percentile(durations, 0.5),
        "resolved_p95_s": percentile(durations, 0.95),
    }


@router.get("/projects/{project_id}/stats")
async def project_stats(
    project_id: int,
    days: int = Query(default=14, ge=1, le=180),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    project = await project_for(db, user, project_id)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    incidents = [i for i in (await db.execute(select(Incident).where(incidents_of(project)))).scalars() if aware(i.created_at) >= since]
    ids = [i.id for i in incidents]
    created = {i.id: aware(i.created_at) for i in incidents}
    events: list[AuditEvent] = []
    if ids:
        events = list(
            (await db.execute(select(AuditEvent).where(AuditEvent.incident_id.in_(ids), AuditEvent.event_type.in_(WANTED)).order_by(AuditEvent.created_at))).scalars()
        )

    per_day = Counter(created[i].date().isoformat() for i in ids)
    days_axis = [(since + timedelta(days=n + 1)).date().isoformat() for n in range(days)]
    initial = [e for e in events if e.event_type == "synthesis_completed"]
    verdicts = Counter(str((e.payload or {}).get("verdict", "unknown")) for e in initial)
    to_report = [(aware(e.created_at) - created[e.incident_id]).total_seconds() for e in initial if e.incident_id in created]
    decisions = Counter(str((e.payload or {}).get("decision")) for e in events if e.event_type == "approval_decided")
    decided = sum(decisions.values())
    revalidated = [e for e in events if e.event_type == "revalidation_completed"]
    passed = sum(1 for e in revalidated if e.ok)
    policy_denied = sum(1 for e in events if e.event_type == "policy_evaluated" and (e.payload or {}).get("allowed") is False)

    pending = 0
    if ids:
        proposals = (
            await db.execute(select(RemediationProposal).where(RemediationProposal.incident_id.in_(ids), RemediationProposal.status == ProposalStatus.AWAITING_APPROVAL.value))
        ).scalars().all()
        decided_keys = {(a.incident_id, a.attempt) for a in (await db.execute(select(Approval).where(Approval.incident_id.in_(ids)))).scalars()}
        pending = sum(1 for p in proposals if (p.incident_id, p.attempt) not in decided_keys)

    return {
        "days": days,
        "incidents_total": len(ids),
        "incidents_per_day": [{"date": d, "incidents": per_day.get(d, 0)} for d in days_axis],
        "reports": {
            "total": len(initial),
            "incomplete": sum(1 for e in initial if (e.payload or {}).get("completeness") == "incomplete"),
            "conflicted": sum(1 for e in initial if (e.payload or {}).get("conflict_status") == "conflicted"),
            "time_to_report_p50_s": percentile(to_report, 0.5),
            "time_to_report_p95_s": percentile(to_report, 0.95),
        },
        "verdicts": [{"verdict": v, "count": c} for v, c in verdicts.most_common()],
        "agent_time": [{"agent": a, **d} for a, d in _durations(events).items()],
        "outcomes": await outcomes(db, ids),
        "approvals": {
            "decided": decided,
            "approval_rate_pct": round(100 * decisions.get("approved", 0) / decided, 1) if decided else None,
            "by_decision": [{"decision": k, "count": v} for k, v in decisions.most_common()],
            "pending": pending,
        },
        "fix_outcomes": {
            "pull_requests_opened": sum(1 for e in events if e.event_type == "pull_request_created" and e.ok),
            "policy_denied": policy_denied,
            "revalidation_passed": passed,
            "revalidation_failed": len(revalidated) - passed,
            "fix_success_pct": round(100 * passed / len(revalidated), 1) if revalidated else None,
            "note": "Fix success is a live proxy for accuracy: the share of re-validated fixes that passed Jenkins.",
        },
    }
