"""Routes for handling signed human reviewer approval and rejection actions."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.audit_models import Approval, ApprovalDecision, AuditEvent, ProposalStatus, RemediationProposal
from backend.db.database import get_db
from backend.events.publisher import publisher
from backend.notifications.tokens import (
    ApprovalTokenPayload,
    ExpiredTokenError,
    InvalidTokenFormatError,
    TamperedTokenError,
    TokenAlreadyUsedError,
    consume_approval_token,
    verify_approval_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalDecisionRequest(BaseModel):
    """Payload for submitting an approval decision via API."""

    model_config = ConfigDict(extra="ignore")

    token: str = Field(..., description="HMAC-signed single-use action token")
    reviewer: str = Field(default="human_reviewer", description="Name/email of reviewer")
    reason: str = Field(default="", description="Optional rationale for decision")


def _render_html_response(title: str, message: str, is_error: bool = False, is_approved: bool = False) -> str:
    theme_color = "#cf222e" if is_error else ("#1f883d" if is_approved else "#57606a")
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>{title}</title>
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background-color: #f6f8fa; }}
        .card {{ background: #fff; padding: 32px 40px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 480px; text-align: center; border-top: 6px solid {theme_color}; }}
        h1 {{ font-size: 20px; color: #24292f; margin-bottom: 12px; }}
        p {{ font-size: 14px; color: #57606a; line-height: 1.5; }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>{title}</h1>
        <p>{message}</p>
      </div>
    </body>
    </html>
    """


@router.get("/decide")
@router.post("/decide")
async def decide_approval(
    request: Request,
    token: Optional[str] = Query(None, description="HMAC-signed single-use action token"),
    decision: Optional[str] = Query(None, description="Approved or rejected (must match token scope)"),
    reviewer: Optional[str] = Query(None, description="Name or identifier of reviewer"),
    reason: Optional[str] = Query(None, description="Optional reason"),
    db: AsyncSession = Depends(get_db),
):
    """Handle clicking an Approve or Reject link in a notification.

    Validates:
    - HMAC signature using constant-time check (rejects tampered links with HTTP 400)
    - 24-hour expiration (rejects expired links with HTTP 410)
    - Single-use consumption (rejects double clicks / reuse with HTTP 409)
    - Matching decision scope

    Updates:
    - Proposal status in database
    - Approvals table
    - Audit log (approval_decided)
    - Publishes 'approval.decided' event to RabbitMQ
    """
    token_str = token
    req_reviewer = reviewer
    req_reason = reason or ""

    # Support JSON body for POST
    if not token_str and request.method == "POST":
        try:
            body = await request.json()
            token_str = body.get("token")
            req_reviewer = req_reviewer or body.get("reviewer")
            req_reason = req_reason or body.get("reason", "")
        except Exception:
            pass

    if not token_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required 'token' parameter",
        )

    # 1. Verify and consume token (checks tampering, expiry, and single-use)
    try:
        token_payload = consume_approval_token(token_str)
    except TamperedTokenError as err:
        logger.warning("Tampered token attempt: %s", err)
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(
                _render_html_response("Link Tampering Detected", str(err), is_error=True),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
    except ExpiredTokenError as err:
        logger.info("Expired token attempt: %s", err)
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(
                _render_html_response("Link Expired", "This remediation action link has expired after 24 hours.", is_error=True),
                status_code=status.HTTP_410_GONE,
            )
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(err))
    except TokenAlreadyUsedError as err:
        logger.info("Double-click / reused token attempt: %s", err)
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(
                _render_html_response("Link Already Used", "This approval link has already been used. Decisions cannot be submitted twice.", is_error=True),
                status_code=status.HTTP_409_CONFLICT,
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err))
    except InvalidTokenFormatError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))

    # Decision scope validation
    if decision and decision.lower() != token_payload.decision.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Token decision scope '{token_payload.decision}' does not match requested decision '{decision}'",
        )

    decided_scope = token_payload.decision
    reviewer_name = req_reviewer or "authorized_reviewer"

    # 2. Check DB proposal state & avoid double decisions
    stmt = (
        select(RemediationProposal)
        .where(
            RemediationProposal.incident_id == token_payload.incident_id,
            RemediationProposal.attempt == token_payload.attempt,
        )
        .order_by(RemediationProposal.id.desc())
    )
    res = await db.execute(stmt)
    proposal = res.scalars().first()

    proposal_id = proposal.id if proposal else None

    # Check if this proposal was already decided in approvals table
    if proposal_id:
        app_stmt = select(Approval).where(Approval.proposal_id == proposal_id)
        app_res = await db.execute(app_stmt)
        existing_approval = app_res.scalars().first()
        if existing_approval:
            logger.info("Proposal %d for incident %s already decided by %s", proposal_id, token_payload.incident_id, existing_approval.reviewer)
            if "text/html" in request.headers.get("accept", ""):
                return HTMLResponse(
                    _render_html_response("Decision Already Recorded", f"This proposal was already decided ({existing_approval.decision}) by {existing_approval.reviewer}.", is_error=True),
                    status_code=status.HTTP_409_CONFLICT,
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Proposal already decided ({existing_approval.decision}) by {existing_approval.reviewer}",
            )

    # 3. Update proposal status
    if proposal:
        if decided_scope == "approved":
            proposal.status = ProposalStatus.APPROVED.value
        else:
            proposal.status = ProposalStatus.REJECTED.value

    # 4. Insert Approval record
    approval_rec = Approval(
        incident_id=token_payload.incident_id,
        proposal_id=proposal_id or 0,
        decision=decided_scope,
        reviewer=reviewer_name,
        reason=req_reason or f"{decided_scope.capitalize()} via notification action link",
        decided_at=datetime.now(timezone.utc),
    )
    db.add(approval_rec)

    # 5. Insert AuditEvent record
    audit_event = AuditEvent(
        incident_id=token_payload.incident_id,
        actor=reviewer_name,
        event_type="approval_decided",
        summary=f"Human reviewer {reviewer_name} recorded decision: {decided_scope}",
        payload={
            "incident_id": token_payload.incident_id,
            "attempt": token_payload.attempt,
            "decision": decided_scope,
            "reviewer": reviewer_name,
            "reason": req_reason,
            "jti": token_payload.jti,
        },
        ok=decided_scope == "approved",
    )
    db.add(audit_event)
    await db.commit()

    # 6. Publish 'approval.decided' event
    await publisher.publish_event(
        "approval.decided",
        {
            "incident_id": token_payload.incident_id,
            "attempt": token_payload.attempt,
            "decision": decided_scope,
            "reviewer": reviewer_name,
            "reason": req_reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    is_approved = decided_scope == "approved"
    action_title = "Remediation Approved" if is_approved else "Remediation Rejected"
    action_msg = (
        f"Proposal for incident {token_payload.incident_id} (attempt #{token_payload.attempt}) has been successfully approved. Draft pull request will be generated."
        if is_approved
        else f"Proposal for incident {token_payload.incident_id} (attempt #{token_payload.attempt}) was rejected. No changes will be applied."
    )

    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            _render_html_response(action_title, action_msg, is_error=False, is_approved=is_approved),
            status_code=status.HTTP_200_OK,
        )

    return {
        "status": "success",
        "decision": decided_scope,
        "incident_id": token_payload.incident_id,
        "attempt": token_payload.attempt,
        "reviewer": reviewer_name,
        "message": action_msg,
    }
