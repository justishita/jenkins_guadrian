"""The shared approval contract: the human gate between a proposed fix and a draft PR.

**Owner:** P3 (the ``approvals`` table and this API). P1 builds the approval UI and
the Slack/Teams/email notifications on top of it; P2's approval dashboards read the
same table. Agreed contract:

* ``GET  /approvals/{incident_id}`` - the proposed fix, its policy verdicts and the
  agents' evidence, i.e. everything a reviewer needs before deciding.
* ``POST /approvals/{incident_id}`` - ``{decision: approved|rejected, reviewer,
  comment}``. Records the decision and publishes ``approval.decided``.
* ``approvals`` table - ``incident_id, attempt, decision, reviewer, comment,
  decided_at``; one decision per incident attempt.

The decision applies to the incident's latest proposal, and only while that proposal
is ``awaiting_approval``: a fix the remediation policy blocked (TC-15) never reaches
a human, and a decided proposal cannot be decided again. Recording is the whole job
here - opening the draft PR stays with ``coordinator.remediation_gate``, which reacts
to ``approval.decided`` and still re-checks the pull-request policy itself.

Both endpoints require ``Authorization: Bearer <APPROVAL_API_TOKEN>`` and refuse
every request while the token is unset.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import logging
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.audit_models import (
    Approval,
    AuditEvent,
    AuditEventType,
    PolicyDecision,
    ProposalStatus,
    RemediationProposal,
)
from backend.db.database import get_db
from backend.db.models import Incident
from backend.events.publisher import publisher
from common.blackboard import BlackboardEntry, get_blackboard
from common.fix_branch import format_fix_branch
from common.redaction import redact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])

ACTOR = "approval_service"


class ApprovalRequest(BaseModel):
    """Body of ``POST /approvals/{incident_id}``."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=128)
    comment: str = Field(default="", max_length=4000)


def verify_approval_token(authorization: Optional[str] = Header(None)) -> None:
    """Constant-time bearer check; with no token configured nobody may approve."""
    expected = settings.APPROVAL_API_TOKEN
    scheme, _, supplied = (authorization or "").partition(" ")
    if not expected or scheme.lower() != "bearer" or not supplied:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing or unauthorized approval token")
    if not hmac.compare_digest(supplied.strip(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid approval token")


async def _incident_or_404(db: AsyncSession, incident_id: UUID) -> Incident:
    incident = (
        await db.execute(select(Incident).where(Incident.id == str(incident_id)))
    ).scalar_one_or_none()
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return incident


async def _latest_proposal(db: AsyncSession, incident_id: UUID) -> RemediationProposal | None:
    return (
        await db.execute(
            select(RemediationProposal)
            .where(RemediationProposal.incident_id == str(incident_id))
            .order_by(RemediationProposal.attempt.desc(), RemediationProposal.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _approval_for(db: AsyncSession, incident_id: UUID, attempt: int) -> Approval | None:
    return (
        await db.execute(
            select(Approval).where(Approval.incident_id == str(incident_id), Approval.attempt == attempt)
        )
    ).scalar_one_or_none()


def _blackboard_view(incident_id: UUID) -> dict[str, Any]:
    """The agents' evidence and the Coordinator's synthesis, keyed by writer.

    Best effort: the fix and its policy verdicts come from the database, so an
    unreadable blackboard leaves the reviewer with less context, not with no page.
    """
    view: dict[str, Any] = {"evidence": {}, "synthesis": None}
    try:
        board = get_blackboard()
        for entry in board.read_all(incident_id, section="evidence"):
            view["evidence"][entry.key] = entry.payload
        synthesis = board.read(incident_id, section="synthesis")
        view["synthesis"] = synthesis.payload if synthesis is not None else None
    except Exception as error:  # noqa: BLE001 - context only; never fail the page
        logger.warning("Could not read blackboard for approval view (%s)", type(error).__name__)
    return view


@router.get("/{incident_id}")
async def get_approval(
    incident_id: UUID,
    _: None = Depends(verify_approval_token),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Show the fix awaiting a decision, with the evidence behind it."""
    incident = await _incident_or_404(db, incident_id)
    proposal = await _latest_proposal(db, incident_id)

    fix: dict[str, Any] | None = None
    policy: list[dict[str, Any]] = []
    approval: Approval | None = None
    if proposal is not None:
        fix = {**proposal.to_dict(), "branch": format_fix_branch(incident_id, proposal.attempt)}
        policy = [
            row.to_dict()
            for row in (
                await db.execute(
                    select(PolicyDecision)
                    .where(PolicyDecision.proposal_id == proposal.id)
                    .order_by(PolicyDecision.id)
                )
            ).scalars()
        ]
        approval = await _approval_for(db, incident_id, proposal.attempt)

    return {
        "incident": incident.to_dict(),
        "awaiting_decision": (
            proposal is not None
            and proposal.status == ProposalStatus.AWAITING_APPROVAL.value
            and approval is None
        ),
        "fix": fix,
        "policy_decisions": policy,
        "approval": approval.to_dict() if approval is not None else None,
        **_blackboard_view(incident_id),
    }


@router.post("/{incident_id}", status_code=status.HTTP_201_CREATED)
async def decide_approval(
    incident_id: UUID,
    body: ApprovalRequest,
    _: None = Depends(verify_approval_token),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record one human decision on the latest proposal and announce it."""
    await _incident_or_404(db, incident_id)
    proposal = await _latest_proposal(db, incident_id)
    if proposal is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="No remediation proposal for this incident")

    existing = await _approval_for(db, incident_id, proposal.attempt)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Attempt {proposal.attempt} was already {existing.decision} by {existing.reviewer}",
        )
    if proposal.status != ProposalStatus.AWAITING_APPROVAL.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Proposal is {proposal.status}; only a proposal awaiting approval can be decided",
        )

    reviewer = body.reviewer.strip()
    if not reviewer:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="reviewer must not be blank")
    comment = redact(body.comment.strip())
    decided_at = datetime.now(timezone.utc)
    branch = format_fix_branch(incident_id, proposal.attempt)

    approval = Approval(
        proposal_id=proposal.id,
        incident_id=str(incident_id),
        attempt=proposal.attempt,
        decision=body.decision,
        reviewer=reviewer,
        comment=comment,
        decided_at=decided_at,
    )
    db.add(approval)
    proposal.status = (
        ProposalStatus.APPROVED.value if body.decision == "approved" else ProposalStatus.REJECTED.value
    )
    db.add(
        AuditEvent(
            incident_id=str(incident_id),
            actor=ACTOR,
            event_type=AuditEventType.APPROVAL_DECIDED.value,
            summary=f"{reviewer} {body.decision} the proposal",
            payload={
                "attempt": proposal.attempt,
                "branch": branch,
                "decision": body.decision,
                "reviewer": reviewer,
                "comment": comment,
            },
            ok=body.decision == "approved",
        )
    )
    try:
        await db.commit()
    except IntegrityError as error:
        # Two reviewers raced; the unique constraint kept the first decision.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"Attempt {proposal.attempt} was already decided"
        ) from error

    event = {
        "incident_id": str(incident_id),
        "attempt": approval.attempt,
        "proposal_id": approval.proposal_id,
        "branch": branch,
        "decision": approval.decision,
        "reviewer": approval.reviewer,
        "comment": approval.comment,
        "decided_at": decided_at.isoformat(),
    }
    try:
        get_blackboard().post(
            BlackboardEntry(
                incident_id=incident_id,
                section="approval",
                key=f"attempt_{approval.attempt}",
                author=ACTOR,
                payload=event,
            )
        )
    except Exception as error:  # noqa: BLE001 - the database row is the record
        logger.warning("Could not post approval to blackboard (%s)", type(error).__name__)

    # The decision is committed either way; a failed publish is reported, not hidden,
    # so the caller can tell the Coordinator was not notified.
    published = await publisher.publish_approval_decided(event)
    if not published:
        logger.error("approval.decided was not published for incident %s", incident_id)

    return {"approval": approval.to_dict(), "branch": branch, "event_published": published}


__all__ = ["ApprovalRequest", "router", "verify_approval_token"]
