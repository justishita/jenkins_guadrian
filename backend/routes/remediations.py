"""Where the Coordinator registers a proposed fix, and later reports what became of it.

**Owner:** P3. This is the live half of the approval contract (``approvals.py`` is the
human half): without it a fix the Coordinator proposed never reached the database, so
nobody was ever asked to review it.

* ``POST  /remediations`` - the Coordinator proposed a fix and the remediation policy
  judged it. Stores the proposal and its verdict; if the verdict allows it, publishes
  ``approval.requested`` so the notifier asks a reviewer. Idempotent per
  ``(incident_id, attempt)``: a retried request returns the stored proposal and never
  notifies twice.
* ``PATCH /remediations/{incident_id}/{attempt}`` - after a decision, the Coordinator
  reports the outcome: the draft PR it opened, or why none was opened.

Audit events are not written here: the Coordinator's remediation gate already records
every step it takes. Both endpoints use the approvals service token
(``Authorization: Bearer $APPROVAL_API_TOKEN``) and refuse everything without it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.audit_models import PolicyDecision, ProposalStatus, RemediationProposal
from backend.db.database import get_db
from backend.db.models import Incident
from backend.events.publisher import publisher
from backend.routes.approvals import verify_approval_token
from common.fix_branch import format_fix_branch
from common.redaction import redact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/remediations", tags=["remediations"])


class PolicyVerdictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: str = Field(default="jenkinsguardians/remediation", min_length=1, max_length=128)
    allowed: bool
    violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RemediationRegistration(BaseModel):
    """A fix the Coordinator proposed, with the remediation policy's verdict on it."""

    model_config = ConfigDict(extra="forbid")

    incident_id: UUID
    attempt: int = Field(default=0, ge=0)
    failure_type: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(default="", max_length=20_000)
    diff: str = Field(default="", max_length=200_000)
    changed_files: list[str] = Field(default_factory=list, max_length=100)
    policy: PolicyVerdictBody


class RemediationOutcome(BaseModel):
    """What happened after the human decided."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pr_created", "pr_blocked", "failed"]
    pull_request_url: str | None = Field(default=None, max_length=500)
    detail: str = Field(default="", max_length=4000)


async def _proposal_for(db: AsyncSession, incident_id: UUID, attempt: int) -> RemediationProposal | None:
    return (
        await db.execute(
            select(RemediationProposal)
            .where(RemediationProposal.incident_id == str(incident_id), RemediationProposal.attempt == attempt)
            .order_by(RemediationProposal.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _approval_request(body: RemediationRegistration, proposal: RemediationProposal) -> dict[str, Any]:
    """The ``approval.requested`` message, in the shape the notifier renders."""
    branch = format_fix_branch(body.incident_id, body.attempt)
    return {
        "incident_id": str(body.incident_id),
        "attempt": body.attempt,
        "proposal_id": proposal.id,
        "branch": branch,
        "failure_type": body.failure_type,
        "confidence": body.confidence,
        "proposed_fix": {
            "rationale": proposal.rationale,
            "diff": proposal.diff,
            "changed_files": list(proposal.changed_files or []),
            "branch": branch,
            "confidence": body.confidence,
            "failure_type": body.failure_type,
        },
        "policy": {"allowed": body.policy.allowed, "warnings": body.policy.warnings},
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_remediation(
    body: RemediationRegistration,
    response: Response,
    _: None = Depends(verify_approval_token),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    incident = (await db.execute(select(Incident).where(Incident.id == str(body.incident_id)))).scalar_one_or_none()
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Incident not found")

    existing = await _proposal_for(db, body.incident_id, body.attempt)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return {"proposal_id": existing.id, "status": existing.status, "created": False, "approval_requested": False}

    proposal = RemediationProposal(
        incident_id=str(body.incident_id),
        proposed_by="code_agent",
        attempt=body.attempt,
        status=(ProposalStatus.AWAITING_APPROVAL if body.policy.allowed else ProposalStatus.POLICY_REJECTED).value,
        failure_type=body.failure_type,
        rationale=redact(body.rationale),
        diff=redact(body.diff),
        changed_files=list(body.changed_files),
        confidence=body.confidence,
    )
    db.add(proposal)
    await db.flush()
    db.add(
        PolicyDecision(
            proposal_id=proposal.id,
            incident_id=str(body.incident_id),
            policy_package=body.policy.package,
            allowed=body.policy.allowed,
            violations=[redact(v) for v in body.policy.violations],
            warnings=[redact(w) for w in body.policy.warnings],
        )
    )
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Proposal already registered") from error

    requested = False
    if body.policy.allowed:
        # Committed first: a notification must never point at a proposal that does not exist.
        requested = await publisher.publish_approval_requested(_approval_request(body, proposal))
        if not requested:
            logger.error("approval.requested was not published for incident %s", body.incident_id)
    return {"proposal_id": proposal.id, "status": proposal.status, "created": True, "approval_requested": requested}


@router.patch("/{incident_id}/{attempt}")
async def report_outcome(
    incident_id: UUID,
    attempt: int,
    body: RemediationOutcome,
    _: None = Depends(verify_approval_token),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    proposal = await _proposal_for(db, incident_id, attempt)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No proposal for this incident attempt")
    if body.status == "pr_created":
        proposal.status = ProposalStatus.PR_CREATED.value
        proposal.pull_request_url = body.pull_request_url
    # pr_blocked / failed: the proposal stays approved; the gate's audit trail says why.
    await db.commit()
    return {"proposal_id": proposal.id, "status": proposal.status, "pull_request_url": proposal.pull_request_url}


__all__ = ["RemediationOutcome", "RemediationRegistration", "router"]
