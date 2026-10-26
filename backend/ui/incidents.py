"""Incidents, reports, reviews and decisions - the read/review API behind the project pages."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.audit_models import Approval, AuditEvent, PolicyDecision, ProposalStatus, RemediationProposal
from backend.db.database import get_db
from backend.db.models import Incident
from backend.db.ui_models import Project, User
from backend.routes.approvals import _approval_for, _blackboard_view, _latest_proposal, apply_decision
from backend.ui import reports
from backend.ui.deps import SYSTEM_INCIDENT, current_user, project_for, require_role, visible_projects
from backend.ui.projects import incidents_of, job_matches
from common.fix_branch import format_fix_branch

router = APIRouter(tags=["ui-incidents"])

MAX_TIMELINE = 500


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=4000)
    human_override: bool = False


async def authorized_incident(db: AsyncSession, user: User, incident_id: UUID) -> tuple[Incident, Project | None]:
    """The incident and its project, if this user may see it; anything else is a plain 404."""
    incident = (await db.execute(select(Incident).where(Incident.id == str(incident_id)))).scalar_one_or_none()
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Incident not found")
    projects = await visible_projects(db, user, include_archived=True)
    matching = [p for p in projects if job_matches(p.jenkins_job, incident.job_name)]
    if matching:
        return incident, max(matching, key=lambda p: len(p.jenkins_job))  # the most specific project
    if user.role == "admin":
        return incident, None
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Incident not found")


def _incident_row(incident: Incident, report: dict | None, proposal: RemediationProposal | None, approval: Approval | None) -> dict[str, Any]:
    return {
        **incident.to_dict(),
        "report": report,
        "proposal_status": proposal.status if proposal else None,
        "approval_decision": approval.decision if approval else None,
    }


async def _proposals_and_approvals(db: AsyncSession, ids: list[str]) -> tuple[dict[str, RemediationProposal], dict[tuple[str, int], Approval]]:
    if not ids:
        return {}, {}
    latest: dict[str, RemediationProposal] = {}
    for proposal in (
        await db.execute(select(RemediationProposal).where(RemediationProposal.incident_id.in_(ids)).order_by(RemediationProposal.attempt, RemediationProposal.id))
    ).scalars():
        latest[proposal.incident_id] = proposal  # ascending order: the last write per incident is the newest
    approvals = {(a.incident_id, a.attempt): a for a in (await db.execute(select(Approval).where(Approval.incident_id.in_(ids)))).scalars()}
    return latest, approvals


@router.get("/projects/{project_id}/incidents")
async def list_incidents(
    project_id: int,
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    verdict: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    project = await project_for(db, user, project_id)
    query = select(Incident).where(incidents_of(project)).order_by(Incident.created_at.desc())
    if status_filter:
        query = query.where(Incident.status == status_filter.upper())
    rows = (await db.execute(query)).scalars().all()
    proposals, approvals = await _proposals_and_approvals(db, [r.id for r in rows])
    items = []
    for incident in rows:
        summary = reports.summarize(reports.latest(incident.id))
        if verdict and (summary is None or summary["verdict"] != verdict):
            continue
        proposal = proposals.get(incident.id)
        items.append(_incident_row(incident, summary, proposal, approvals.get((incident.id, proposal.attempt)) if proposal else None))
    return {"total": len(items), "items": items[offset : offset + limit]}


@router.get("/incidents/{incident_id}")
async def incident_detail(incident_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    incident, project = await authorized_incident(db, user, incident_id)
    proposal = await _latest_proposal(db, incident_id)
    approval = await _approval_for(db, incident_id, proposal.attempt) if proposal else None
    policy: list[dict[str, Any]] = []
    if proposal is not None:
        policy = [
            row.to_dict()
            for row in (await db.execute(select(PolicyDecision).where(PolicyDecision.proposal_id == proposal.id).order_by(PolicyDecision.id))).scalars()
        ]
    envelopes = reports.revisions(incident_id)
    timeline = (
        await db.execute(
            select(AuditEvent)
            .where(AuditEvent.incident_id == str(incident_id), AuditEvent.incident_id != SYSTEM_INCIDENT)
            .order_by(AuditEvent.created_at, AuditEvent.id)
            .limit(MAX_TIMELINE)
        )
    ).scalars()
    evidence = {agent: reports.trim_evidence(doc) for agent, doc in _blackboard_view(incident_id)["evidence"].items()}
    awaiting = (
        proposal is not None and proposal.status == ProposalStatus.AWAITING_APPROVAL.value and approval is None
    )
    return {
        "incident": incident.to_dict(),
        "project": project.to_dict() if project else None,
        "report": envelopes[-1] if envelopes else None,
        "revisions": [reports.summarize(e) for e in envelopes],
        "evidence": evidence,
        "fix": {**proposal.to_dict(), "branch": format_fix_branch(incident_id, proposal.attempt)} if proposal else None,
        "policy_decisions": policy,
        "approval": approval.to_dict() if approval else None,
        "awaiting_decision": awaiting,
        "can_decide": awaiting and user.role in ("reviewer", "admin"),
        "timeline": [e.to_dict() for e in timeline],
        "links": {
            "jenkins_build": incident.build_url,
            "pull_request": proposal.pull_request_url if proposal else None,
            "grafana": f"{settings.GRAFANA_PUBLIC_URL.rstrip('/')}/d/agent-ops?var-incident={incident.id}",
        },
    }


@router.get("/incidents/{incident_id}/report")
async def incident_report(
    incident_id: UUID,
    format: Literal["json", "md"] = "json",
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await authorized_incident(db, user, incident_id)
    envelope = reports.latest(incident_id)
    if envelope is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No report has been written for this incident yet")
    if format == "md":
        return PlainTextResponse(reports.render_markdown(envelope), media_type="text/markdown")
    return envelope


@router.get("/projects/{project_id}/reports")
async def list_reports(
    project_id: int,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    project = await project_for(db, user, project_id)
    incidents = (await db.execute(select(Incident).where(incidents_of(project)).order_by(Incident.created_at.desc()))).scalars().all()
    items = []
    for incident in incidents:
        summary = reports.summarize(reports.latest(incident.id))
        if summary is not None:
            items.append({"incident": incident.to_dict(), "report": summary})
    return {"total": len(items), "items": items[offset : offset + limit]}


@router.get("/projects/{project_id}/reviews")
async def pending_reviews(project_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """Fixes waiting for a human, newest first."""
    project = await project_for(db, user, project_id)
    rows = (
        await db.execute(
            select(RemediationProposal, Incident)
            .join(Incident, Incident.id == RemediationProposal.incident_id)
            .where(incidents_of(project), RemediationProposal.status == ProposalStatus.AWAITING_APPROVAL.value)
            .order_by(RemediationProposal.created_at.desc())
        )
    ).all()
    result = []
    for proposal, incident in rows:
        if await _approval_for(db, UUID(incident.id), proposal.attempt) is not None:
            continue
        result.append(
            {
                "incident": incident.to_dict(),
                "fix": {"attempt": proposal.attempt, "failure_type": proposal.failure_type, "confidence": proposal.confidence, "rationale": proposal.rationale, "changed_files": proposal.changed_files},
                "report": reports.summarize(reports.latest(incident.id)),
            }
        )
    return result


@router.post("/incidents/{incident_id}/decision", status_code=status.HTTP_201_CREATED)
async def decide(
    incident_id: UUID,
    body: DecisionBody,
    user: User = Depends(require_role("reviewer")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Approve or reject the latest fix. The reviewer is the signed-in user, never a typed name."""
    await authorized_incident(db, user, incident_id)
    proposal = await _latest_proposal(db, incident_id)
    if proposal is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="No remediation proposal for this incident")
    if body.decision == "rejected" and not body.comment.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Say why you are rejecting this fix")
    if body.human_override and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only an admin can override a policy refusal")
    approval, branch, published = await apply_decision(db, incident_id, proposal, body.decision, user.email, body.comment, body.human_override)
    return {"approval": approval.to_dict(), "branch": branch, "event_published": published}
