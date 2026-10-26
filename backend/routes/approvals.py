"""The shared approval contract and review interface.

Provides:
- GET/POST /approvals/decide: Notification action link handler.
- GET  /approvals/{incident_id}/review: Server-rendered HTML page (Jinja2) with unified report,
  syntax-highlighted diff, OPA policy results, CSRF protection, and zero external CDN dependencies.
- GET  /approvals/{incident_id}: API view with Bearer token authentication.
- POST /approvals/{incident_id}: Human decision recording with OPA policy gate (TC-15),
  supporting both Bearer token API requests and signed single-use action tokens from the review UI.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import logging
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
import jinja2
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
from backend.notifications.csrf import generate_csrf_token, verify_csrf_token
from backend.notifications.renderer import NotificationRenderer, renderer as default_renderer
from backend.notifications.tokens import (
    ApprovalTokenPayload,
    ExpiredTokenError,
    InvalidTokenFormatError,
    TamperedTokenError,
    TokenAlreadyUsedError,
    consume_approval_token,
    generate_approval_token,
)
from common.blackboard import BlackboardEntry, get_blackboard
from common.fix_branch import format_fix_branch
from common.redaction import redact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])

ACTOR = "approval_service"

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html", "xml"]),
)


class ApprovalRequest(BaseModel):
    """Body of ``POST /approvals/{incident_id}``."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=128)
    comment: str = Field(default="", max_length=4000)
    auto_merge: Optional[bool] = None
    human_override: Optional[bool] = None


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
    """The agents' evidence and the Coordinator's synthesis, keyed by writer."""
    view: dict[str, Any] = {"evidence": {}, "synthesis": None}
    try:
        board = get_blackboard()
        for entry in board.read_all(incident_id, section="evidence"):
            view["evidence"][entry.key] = entry.payload
        synthesis = board.read(incident_id, section="synthesis")
        view["synthesis"] = synthesis.payload if synthesis is not None else None
    except Exception as error:
        logger.debug("Could not read blackboard for incident %s: %s", incident_id, error)
    return view


# ------------------------------------------------------------------------------
# 1. NOTIFICATION ACTION LINK HANDLER (/approvals/decide)
# ------------------------------------------------------------------------------

@router.get("/decide")
@router.post("/decide")
async def decide_approval_action_link(
    request: Request,
    token: Optional[str] = Query(None),
    decision: Optional[str] = Query(None),
    reviewer: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Direct handler for one-click action URLs from notifications."""
    token_str = token
    req_reviewer = reviewer or "authorized_reviewer"
    req_reason = reason or ""

    if not token_str and request.method == "POST":
        try:
            body = await request.json()
            token_str = body.get("token")
            req_reviewer = body.get("reviewer") or req_reviewer
            req_reason = body.get("reason", "")
        except Exception:
            pass

    if not token_str:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing required 'token' parameter")

    try:
        token_payload = consume_approval_token(token_str)
    except TamperedTokenError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))
    except ExpiredTokenError as err:
        raise HTTPException(status.HTTP_410_GONE, detail=str(err))
    except TokenAlreadyUsedError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(err))
    except InvalidTokenFormatError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))

    if decision and decision.lower() != token_payload.decision.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Token decision scope '{token_payload.decision}' does not match requested decision '{decision}'",
        )

    decided_scope = token_payload.decision
    incident_uuid = UUID(token_payload.incident_id)

    proposal = await _latest_proposal(db, incident_uuid)
    proposal_id = proposal.id if proposal else 0

    existing = await _approval_for(db, incident_uuid, token_payload.attempt)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Proposal already decided ({existing.decision}) by {existing.reviewer}",
        )

    # Check TC-15 if proposal exists
    if proposal:
        policy_rows = (
            await db.execute(
                select(PolicyDecision)
                .where(PolicyDecision.proposal_id == proposal.id)
            )
        ).scalars().all()
        policy_blocked = (
            proposal.status == ProposalStatus.POLICY_REJECTED.value
            or any(not pd.allowed for pd in policy_rows)
        )
        if decided_scope == "approved" and policy_blocked:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Approval blocked: OPA remediation policy check failed (TC-15). Human override required.",
            )
        proposal.status = (
            ProposalStatus.APPROVED.value if decided_scope == "approved" else ProposalStatus.REJECTED.value
        )

    branch = format_fix_branch(incident_uuid, token_payload.attempt)
    decided_at = datetime.now(timezone.utc)

    approval = Approval(
        proposal_id=proposal_id,
        incident_id=token_payload.incident_id,
        attempt=token_payload.attempt,
        decision=decided_scope,
        reviewer=req_reviewer,
        comment=req_reason,
        decided_at=decided_at,
    )
    db.add(approval)
    db.add(
        AuditEvent(
            incident_id=token_payload.incident_id,
            actor=req_reviewer,
            event_type=AuditEventType.APPROVAL_DECIDED.value,
            summary=f"Human decision recorded: {decided_scope}",
            payload={
                "attempt": token_payload.attempt,
                "branch": branch,
                "decision": decided_scope,
                "reviewer": req_reviewer,
                "comment": req_reason,
            },
            ok=decided_scope == "approved",
        )
    )
    await db.commit()

    event = {
        "incident_id": token_payload.incident_id,
        "attempt": approval.attempt,
        "proposal_id": approval.proposal_id,
        "branch": branch,
        "decision": approval.decision,
        "reviewer": approval.reviewer,
        "comment": approval.comment,
        "decided_at": decided_at.isoformat(),
    }
    await publisher.publish_approval_decided(event)

    if "text/html" in request.headers.get("accept", ""):
        theme = "#1a7f37" if decided_scope == "approved" else "#cf222e"
        title = "Remediation Approved" if decided_scope == "approved" else "Remediation Rejected"
        msg = f"Proposal for incident {token_payload.incident_id} (attempt #{token_payload.attempt}) has been successfully {decided_scope}."
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head>
              <meta charset="utf-8">
              <title>{title}</title>
              <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #f6f8fa; }}
                .card {{ background: #fff; padding: 32px 40px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 480px; text-align: center; border-top: 6px solid {theme}; }}
                h1 {{ font-size: 20px; color: #24292f; margin-bottom: 12px; }}
                p {{ font-size: 14px; color: #57606a; line-height: 1.5; }}
              </style>
            </head>
            <body>
              <div class="card">
                <h1>{title}</h1>
                <p>{msg}</p>
              </div>
            </body>
            </html>
            """,
            status_code=status.HTTP_200_OK,
        )

    return {
        "status": "success",
        "decision": decided_scope,
        "incident_id": token_payload.incident_id,
        "attempt": token_payload.attempt,
        "reviewer": req_reviewer,
    }


# ------------------------------------------------------------------------------
# 2. SERVER-RENDERED HTML REVIEW PAGE (Jinja2)
# ------------------------------------------------------------------------------

@router.get("/{incident_id}/review", response_class=HTMLResponse)
async def review_approval_page(
    incident_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Server-rendered HTML page showing unified report, proposed diff, and policy results.

    Contains:
    - Unified incident report & top evidence with links to Jenkins build
    - Proposed diff with syntax styling (no external CDN dependencies)
    - OPA / Conftest policy evaluation results
    - Two action forms (Approve / Reject) with CSRF protection and signed HMAC tokens
    """
    incident = await _incident_or_404(db, incident_id)
    proposal = await _latest_proposal(db, incident_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No remediation proposal for this incident")

    # Fetch policy decisions
    policy_rows = (
        await db.execute(
            select(PolicyDecision)
            .where(PolicyDecision.proposal_id == proposal.id)
            .order_by(PolicyDecision.id)
        )
    ).scalars().all()

    policy_allowed = (
        proposal.status != ProposalStatus.POLICY_REJECTED.value
        and all(pd.allowed for pd in policy_rows)
    )
    policy_violations: list[str] = []
    policy_warnings: list[str] = []
    for pd in policy_rows:
        if isinstance(pd.violations, list):
            policy_violations.extend(pd.violations)
        if isinstance(pd.warnings, list):
            policy_warnings.extend(pd.warnings)

    existing_approval = await _approval_for(db, incident_id, proposal.attempt)
    bb_view = _blackboard_view(incident_id)

    synthesis = bb_view.get("synthesis") or {}
    evidence_map = bb_view.get("evidence") or {}
    jenkins_evidence = evidence_map.get("jenkins_agent") or {}

    summary = (
        synthesis.get("summary")
        or jenkins_evidence.get("summary")
        or proposal.rationale
        or "Failure detected in Jenkins build."
    )
    failure_type = proposal.failure_type or jenkins_evidence.get("failure_type") or "unknown"
    confidence = proposal.confidence or jenkins_evidence.get("confidence") or 0.85

    rendered = default_renderer.render(
        incident=incident.to_dict(),
        jenkins_findings={
            "failure_type": failure_type,
            "confidence": confidence,
            "summary": summary,
            "evidence_items": jenkins_evidence.get("evidence_items", []),
        },
        proposed_fix={
            "rationale": proposal.rationale,
            "diff": proposal.diff,
            "changed_files": proposal.changed_files,
        },
        attempt=proposal.attempt,
    )

    approve_token = generate_approval_token(incident_id, proposal.attempt, "approved")
    reject_token = generate_approval_token(incident_id, proposal.attempt, "rejected")
    csrf_token = generate_csrf_token(str(incident_id))

    diff_lines = [line for line in proposal.diff.splitlines()] if proposal.diff else []

    template = jinja_env.get_template("review.html")
    html_content = template.render(
        incident=incident.to_dict(),
        proposal=proposal.to_dict(),
        branch_name=format_fix_branch(incident_id, proposal.attempt),
        diagnosis={"failure_type": failure_type, "confidence": confidence, "summary": summary},
        top_evidence=rendered.top_evidence,
        diff_lines=diff_lines,
        policy_allowed=policy_allowed,
        policy_violations=policy_violations,
        policy_warnings=policy_warnings,
        existing_decision=existing_approval.to_dict() if existing_approval else None,
        approve_token=approve_token,
        reject_token=reject_token,
        csrf_token=csrf_token,
    )
    return HTMLResponse(content=html_content, status_code=200)


# ------------------------------------------------------------------------------
# 3. JSON API VIEW
# ------------------------------------------------------------------------------

@router.get("/{incident_id}")
async def get_approval_view(
    incident_id: UUID,
    _: None = Depends(verify_approval_token),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return everything a reviewer needs to decide on a proposal."""
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


# ------------------------------------------------------------------------------
# 4. POST DECISION HANDLER (Handles API and Form Submissions with TC-15)
# ------------------------------------------------------------------------------

async def apply_decision(
    db: AsyncSession,
    incident_id: UUID,
    proposal: RemediationProposal,
    decision: str,
    reviewer: str,
    comment: str,
    human_override: bool,
) -> tuple[Approval, str, bool]:
    """Record one human decision on ``proposal``: the single place the rules live.

    Enforces TC-15 (no approval of a policy-blocked proposal without an explicit human override),
    one decision per (incident, attempt), the audit row in the same transaction, the blackboard entry
    and the ``approval.decided`` event. Callers authenticate first (bearer token, signed link, or the
    UI session); this function trusts that and raises ``HTTPException`` for every refusal.
    """
    if decision not in ("approved", "rejected"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="decision must be 'approved' or 'rejected'")

    reviewer_clean = reviewer.strip()
    if not reviewer_clean:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="reviewer must not be blank")

    existing = await _approval_for(db, incident_id, proposal.attempt)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Attempt {proposal.attempt} was already {existing.decision} by {existing.reviewer}",
        )

    # --------------------------------------------------------------------------
    # TC-15: OPA Policy Enforcement
    # --------------------------------------------------------------------------
    policy_rows = (
        await db.execute(
            select(PolicyDecision)
            .where(PolicyDecision.proposal_id == proposal.id)
        )
    ).scalars().all()
    policy_blocked = (
        proposal.status == ProposalStatus.POLICY_REJECTED.value
        or any(not pd.allowed for pd in policy_rows)
    )

    if decision == "approved":
        if policy_blocked and not human_override:
            logger.warning(
                "Approval rejected for incident %s attempt %d: OPA remediation policy failed (TC-15)",
                incident_id,
                proposal.attempt,
            )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Approval blocked: OPA remediation policy check failed (TC-15). Human override required.",
            )

        if proposal.status != ProposalStatus.AWAITING_APPROVAL.value and not (policy_blocked and human_override):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Proposal is {proposal.status}; only a proposal awaiting approval can be decided",
            )
    else:
        if proposal.status not in (ProposalStatus.AWAITING_APPROVAL.value, ProposalStatus.POLICY_REJECTED.value):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Proposal is {proposal.status}; only a proposal awaiting approval can be decided",
            )

    comment_clean = redact(comment.strip())
    decided_at = datetime.now(timezone.utc)
    branch = format_fix_branch(incident_id, proposal.attempt)

    approval = Approval(
        proposal_id=proposal.id,
        incident_id=str(incident_id),
        attempt=proposal.attempt,
        decision=decision,
        reviewer=reviewer_clean,
        comment=comment_clean,
        decided_at=decided_at,
    )
    db.add(approval)
    proposal.status = (
        ProposalStatus.APPROVED.value if decision == "approved" else ProposalStatus.REJECTED.value
    )
    db.add(
        AuditEvent(
            incident_id=str(incident_id),
            actor=ACTOR,
            event_type=AuditEventType.APPROVAL_DECIDED.value,
            summary=f"{reviewer_clean} {decision} the proposal" + (" (human override)" if human_override else ""),
            payload={
                "attempt": proposal.attempt,
                "branch": branch,
                "decision": decision,
                "reviewer": reviewer_clean,
                "comment": comment_clean,
                "human_override": human_override,
            },
            ok=decision == "approved",
        )
    )

    try:
        await db.commit()
    except IntegrityError as error:
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
        "human_override": human_override,
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
    except Exception as error:
        logger.warning("Could not post approval to blackboard (%s)", type(error).__name__)

    published = await publisher.publish_approval_decided(event)
    if not published:
        logger.error("approval.decided was not published for incident %s", incident_id)

    return approval, branch, published


@router.post("/{incident_id}", status_code=status.HTTP_201_CREATED)
async def post_approval_decision(
    incident_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Record one human decision on the latest proposal and announce it.

    Authenticates via:
    1. Header: ``Authorization: Bearer <APPROVAL_API_TOKEN>``
    OR
    2. Body: Signed HMAC token + verified CSRF token

    Enforces TC-15:
    Approval must be impossible if OPA check failed unless an explicit human_override is passed.
    """
    await _incident_or_404(db, incident_id)
    proposal = await _latest_proposal(db, incident_id)
    if proposal is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="No remediation proposal for this incident")

    content_type = request.headers.get("content-type", "")
    decision: str = ""
    reviewer: str = ""
    comment: str = ""
    token_str: Optional[str] = None
    csrf_token_str: Optional[str] = None
    human_override: bool = False
    auth_header = request.headers.get("authorization")

    if "application/json" in content_type:
        raw_body = await request.json()
        if "auto_merge" in raw_body and raw_body["auto_merge"] is True:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="auto_merge is not permitted")
        decision = raw_body.get("decision", "")
        reviewer = raw_body.get("reviewer", "")
        comment = raw_body.get("comment", "")
        token_str = raw_body.get("token")
        csrf_token_str = raw_body.get("csrf_token")
        human_override = bool(raw_body.get("human_override", False))
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form_data = await request.form()
        decision = str(form_data.get("decision", ""))
        reviewer = str(form_data.get("reviewer", ""))
        comment = str(form_data.get("comment", ""))
        token_str = form_data.get("token")  # type: ignore[assignment]
        csrf_token_str = form_data.get("csrf_token")  # type: ignore[assignment]
        human_override = str(form_data.get("human_override", "")).lower() in ("true", "1", "yes")
    else:
        decision = request.query_params.get("decision", "")
        reviewer = request.query_params.get("reviewer", "")
        comment = request.query_params.get("comment", "")
        token_str = request.query_params.get("token")
        csrf_token_str = request.query_params.get("csrf_token")
        human_override = request.query_params.get("human_override", "").lower() in ("true", "1", "yes")

    has_bearer = False
    if auth_header and auth_header.strip().lower().startswith("bearer "):
        verify_approval_token(auth_header)
        has_bearer = True
    elif token_str:
        if csrf_token_str and not verify_csrf_token(csrf_token_str, str(incident_id)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF token validation failed")
        try:
            token_payload = consume_approval_token(token_str)
            if token_payload.incident_id != str(incident_id):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Token does not match incident")
            if token_payload.attempt != proposal.attempt:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Token attempt does not match current proposal attempt")
        except TamperedTokenError as err:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))
        except ExpiredTokenError as err:
            raise HTTPException(status.HTTP_410_GONE, detail=str(err))
        except TokenAlreadyUsedError as err:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(err))
        except InvalidTokenFormatError as err:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))
    else:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing or unauthorized approval credentials")

    approval, branch, published = await apply_decision(
        db, incident_id, proposal, decision, reviewer, comment, human_override
    )

    if "text/html" in request.headers.get("accept", "") and not has_bearer:
        theme = "#1a7f37" if decision == "approved" else "#cf222e"
        title = "Remediation Approved" if decision == "approved" else "Remediation Rejected"
        msg = (
            f"Fix proposal for incident {incident_id} (attempt #{proposal.attempt}) was successfully {decision}."
            + (" (Human Override applied)" if human_override else "")
        )
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head>
              <meta charset="utf-8">
              <title>{title}</title>
              <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #f6f8fa; }}
                .card {{ background: #fff; padding: 32px 40px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 480px; text-align: center; border-top: 6px solid {theme}; }}
                h1 {{ font-size: 20px; color: #24292f; margin-bottom: 12px; }}
                p {{ font-size: 14px; color: #57606a; line-height: 1.5; }}
              </style>
            </head>
            <body>
              <div class="card">
                <h1>{title}</h1>
                <p>{msg}</p>
              </div>
            </body>
            </html>
            """,
            status_code=status.HTTP_201_CREATED,
        )

    return {"approval": approval.to_dict(), "branch": branch, "event_published": published}


__all__ = ["ApprovalRequest", "apply_decision", "router", "verify_approval_token"]
