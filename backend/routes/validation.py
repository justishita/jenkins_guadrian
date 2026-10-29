"""Jenkins re-validation webhook endpoint and remediation lifecycle management."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any, Optional, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.audit_models import AuditEvent, AuditEventType, ProposalStatus, RemediationProposal
from backend import incident_lifecycle as lifecycle
from backend.db.database import get_db
from backend.db.models import Incident
from backend.events.publisher import publisher
from backend.routes.webhooks import _read_limited_body, verify_webhook_token
from common.blackboard import BlackboardEntry, get_blackboard
from common.redaction import redact_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["validation"])

MAX_ATTEMPTS_DEFAULT = 2
_VALIDATION_LOCK = asyncio.Lock()
_PROCESSED_KEYS: set[str] = set()


class ValidationWebhookPayload(BaseModel):
    """Payload for Jenkins build validation webhooks."""

    model_config = ConfigDict(extra="ignore")

    job_name: str = Field(..., description="Name of the Jenkins validation job")
    build_number: int = Field(default=0, description="Build number of the validation run")
    result: str = Field(..., description="Result of the validation build (e.g. SUCCESS, FAILURE)")
    branch: Optional[str] = Field(default=None, description="Git branch name")
    change_id: Optional[Union[str, int]] = Field(default=None, description="PR or change identifier")
    incident_id: Optional[str] = Field(default=None, description="Direct incident ID if known")
    remediation_attempt: Optional[int] = Field(default=None, description="Remediation attempt number")
    build_url: Optional[str] = Field(default=None, description="URL of the validation build")


def version_incident_evidence(incident_id: str | UUID, attempt: int) -> None:
    """Ensure older evidence for this incident is versioned/archived per attempt."""
    inc_str = str(incident_id)

    # 1. Archive in data/evidence
    evidence_dir = Path("data") / "evidence" / inc_str
    if evidence_dir.exists():
        for f in evidence_dir.glob("*.json"):
            if ".attempt_" in f.name or ".v" in f.name:
                continue
            archive_copy = evidence_dir / f"{f.stem}.attempt_{attempt}{f.suffix}"
            if not archive_copy.exists():
                try:
                    shutil.copy2(f, archive_copy)
                    logger.info("Archived evidence %s to %s", f.name, archive_copy.name)
                except Exception as exc:
                    logger.warning("Could not archive evidence file %s: %s", f, exc)

    # 2. Archive in data/blackboard
    bb_dir = Path("data") / "blackboard" / inc_str
    if bb_dir.exists():
        history_file = bb_dir / "history.jsonl"
        if history_file.exists():
            archive_hist = bb_dir / f"history_attempt_{attempt}.jsonl"
            if not archive_hist.exists():
                try:
                    shutil.copy2(history_file, archive_hist)
                except Exception as exc:
                    logger.warning("Could not archive blackboard history for %s: %s", inc_str, exc)


async def _resolve_incident_id(
    payload: ValidationWebhookPayload,
    db: AsyncSession,
) -> tuple[Optional[str], Optional[int]]:
    """Resolve incident ID and attempt from payload.

    Priority:
    1. Explicit incident_id
    2. Branch name matching 'ai-fix/<incident_id>-<attempt>'
    3. PR change_id lookup in RemediationProposal table
    """
    resolved_id: Optional[str] = None
    resolved_attempt: Optional[int] = payload.remediation_attempt

    # 1. Prefer explicit incident_id
    if payload.incident_id and str(payload.incident_id).strip():
        resolved_id = str(payload.incident_id).strip()

    # 2. Parse branch name 'ai-fix/<incident_id>-<attempt>'
    if not resolved_id and payload.branch:
        clean_branch = payload.branch.replace("refs/heads/", "").replace("origin/", "").strip()
        if "ai-fix/" in clean_branch:
            sub = clean_branch.split("ai-fix/", 1)[1]
            match = re.match(r"^(.*?)(?:-(\d+))?$", sub)
            if match:
                resolved_id = match.group(1).strip()
                if match.group(2) and resolved_attempt is None:
                    try:
                        resolved_attempt = int(match.group(2))
                    except ValueError:
                        pass

    # 3. Look up by PR change_id in the remediation table
    if not resolved_id and payload.change_id is not None:
        change_id_str = str(payload.change_id).strip()
        query = (
            select(RemediationProposal)
            .where(
                or_(
                    RemediationProposal.pull_request_url == change_id_str,
                    RemediationProposal.pull_request_url.like(f"%/{change_id_str}"),
                    RemediationProposal.pull_request_url.like(f"%pull/{change_id_str}"),
                    RemediationProposal.id == int(change_id_str) if change_id_str.isdigit() else False,
                )
            )
            .order_by(RemediationProposal.created_at.desc())
            .limit(1)
        )
        result = await db.execute(query)
        proposal = result.scalar_one_or_none()
        if proposal:
            resolved_id = proposal.incident_id
            if resolved_attempt is None:
                resolved_attempt = proposal.attempt

    return resolved_id, resolved_attempt


@router.post("/jenkins/validation", status_code=status.HTTP_202_ACCEPTED)
async def handle_jenkins_validation(
    request: Request,
    _: None = Depends(verify_webhook_token),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Receive and process Jenkins remediation re-validation build results.

    - Resolves incident_id via explicit ID, 'ai-fix/<incident_id>-<attempt>' branch, or PR change_id.
    - If not resolvable, logs 'orphan validation' and returns 202.
    - Idempotently records validation result in database, audit trail, and blackboard.
    - Versions existing evidence (attempt 1 vs 2) so the audit trail is preserved.
    - Publishes 'validation.completed' on the incidents exchange.
    - On FAILURE with attempt < MAX_ATTEMPTS (default 2), publishes 'incident.reinvestigate'.
    - On FAILURE with attempt >= MAX_ATTEMPTS, marks incident ESCALATED for human intervention.
    """
    body = await _read_limited_body(request)
    try:
        raw_json = json.loads(body)
        if not isinstance(raw_json, dict):
            raise ValueError("Payload must be a JSON object")
        payload = ValidationWebhookPayload.model_validate(redact_value(raw_json))
    except Exception as error:
        logger.warning("Invalid Jenkins validation payload (%s)", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid validation payload format",
        ) from error

    async with _VALIDATION_LOCK:
        # 1. Resolve incident ID and attempt
        resolved_id, resolved_attempt = await _resolve_incident_id(payload, db)

        if not resolved_id:
            logger.warning("orphan validation: could not resolve incident from payload: %s", payload.model_dump(mode="json"))
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "validation_received",
                    "message": "orphan validation",
                },
            )

        # 2. Verify incident exists in DB
        inc_result = await db.execute(select(Incident).where(Incident.id == resolved_id))
        incident = inc_result.scalar_one_or_none()
        if incident is None:
            logger.warning("orphan validation: incident %s not found in incidents table", resolved_id)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "validation_received",
                    "message": "orphan validation",
                    "incident_id": resolved_id,
                },
            )

        if resolved_attempt is None:
            resolved_attempt = incident.remediation_attempt or 0

        # 3. Idempotency check: duplicate event handling
        val_key = f"{resolved_id}:{payload.job_name}:{payload.build_number}:{payload.result.upper()}:{resolved_attempt}"
        if val_key in _PROCESSED_KEYS:
            logger.info("Duplicate validation received for incident %s, build %s", resolved_id, payload.build_number)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "already_processed",
                    "incident_id": resolved_id,
                    "message": "Validation already processed for this build",
                },
            )

        existing_audit = await db.execute(
            select(AuditEvent).where(
                AuditEvent.incident_id == resolved_id,
                AuditEvent.event_type == AuditEventType.REVALIDATION_COMPLETED.value,
            )
        )
        for row in existing_audit.scalars().all():
            p = row.payload or {}
            if (
                p.get("build_number") == payload.build_number
                and p.get("job_name") == payload.job_name
                and str(p.get("result", "")).upper() == payload.result.upper()
            ):
                _PROCESSED_KEYS.add(val_key)
                logger.info("Duplicate validation audit record found for incident %s", resolved_id)
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "status": "already_processed",
                        "incident_id": resolved_id,
                        "message": "Validation already processed for this build",
                    },
                )

        _PROCESSED_KEYS.add(val_key)

        # 4. Version the old evidence before modifying incident state
        version_incident_evidence(resolved_id, resolved_attempt)

        is_success = str(payload.result).strip().upper() in {"SUCCESS", "PASSED", "FIXED"}
        max_attempts_env = int(os.getenv("MAX_ATTEMPTS", str(MAX_ATTEMPTS_DEFAULT)))

        # 5. Persist validation status on proposal and incident
        prop_result = await db.execute(
            select(RemediationProposal)
            .where(RemediationProposal.incident_id == resolved_id)
            .order_by(RemediationProposal.created_at.desc())
            .limit(1)
        )
        proposal = prop_result.scalar_one_or_none()
        if proposal:
            proposal.status = (
                ProposalStatus.VALIDATION_PASSED.value
                if is_success
                else ProposalStatus.VALIDATION_FAILED.value
            )

        audit_summary = f"Validation build #{payload.build_number} finished with {payload.result}"
        audit_payload = {
            "job_name": payload.job_name,
            "build_number": payload.build_number,
            "result": payload.result,
            "branch": payload.branch,
            "attempt": resolved_attempt,
            "change_id": str(payload.change_id) if payload.change_id is not None else None,
        }

        audit_entry = AuditEvent(
            incident_id=resolved_id,
            actor="jenkins",
            event_type=AuditEventType.REVALIDATION_COMPLETED.value,
            summary=audit_summary,
            ok=is_success,
            payload=audit_payload,
        )
        db.add(audit_entry)

        # 6. Post validation result to Blackboard
        try:
            get_blackboard().post(
                BlackboardEntry(
                    incident_id=UUID(str(resolved_id)),
                    section="validation",
                    key="jenkins",
                    author="backend",
                    payload={
                        "job_name": payload.job_name,
                        "build_number": payload.build_number,
                        "result": payload.result,
                        "attempt": resolved_attempt,
                        "status": "SUCCESS" if is_success else "FAILURE",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
            )
        except Exception as e:
            logger.warning("Could not post validation entry to blackboard: %s", e)

        # 7. Publish 'validation.completed' on the incidents exchange
        val_completed_event = {
            "incident_id": resolved_id,
            "status": "SUCCESS" if is_success else "FAILURE",
            "result": payload.result,
            "job_name": payload.job_name,
            "build_number": payload.build_number,
            "attempt": resolved_attempt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await publisher.publish_validation_completed(val_completed_event)

        # 8. Success vs Failure (reinvestigate or escalate)
        if is_success:
            lifecycle.transition(
                db, incident, lifecycle.RESOLVED, reason=f"re-validation build #{payload.build_number} passed", actor="jenkins", attempt=resolved_attempt
            )
            await db.commit()
            await db.refresh(incident)
            logger.info("Incident %s validation succeeded; marked RESOLVED", resolved_id)
        else:
            if resolved_attempt < max_attempts_env:
                next_attempt = resolved_attempt + 1
                incident.remediation_attempt = next_attempt
                lifecycle.transition(
                    db,
                    incident,
                    lifecycle.OPEN,
                    reason=f"re-validation failed ({payload.result}); re-investigating attempt {next_attempt}",
                    actor="jenkins",
                    attempt=next_attempt,
                )
                await db.commit()
                await db.refresh(incident)

                prev_summary = f"Validation attempt {resolved_attempt} failed with {payload.result}"
                if proposal and proposal.rationale:
                    prev_summary = f"{proposal.rationale} (Validation failed: {payload.result})"
                else:
                    try:
                        ev_entry = get_blackboard().read(
                            UUID(str(resolved_id)), section="evidence", key="jenkins_agent"
                        )
                        if ev_entry and isinstance(ev_entry.payload, dict) and ev_entry.payload.get("summary"):
                            prev_summary = f"{ev_entry.payload['summary']} (Validation failed: {payload.result})"
                    except Exception:
                        pass

                new_build_url = (
                    payload.build_url
                    or (
                        f"{settings.JENKINS_URL.rstrip('/')}/job/{payload.job_name}/{payload.build_number}/"
                        if payload.build_number and settings.JENKINS_URL
                        else f"http://jenkins:8080/job/{payload.job_name}/{payload.build_number}/"
                        if payload.build_number
                        else ""
                    )
                )

                reinvestigate_event = {
                    "incident_id": resolved_id,
                    "attempt": next_attempt,
                    "previous_failure_summary": prev_summary,
                    "new_build_url": new_build_url,
                }
                await publisher.publish_incident_reinvestigate(reinvestigate_event)
                logger.info("Published incident.reinvestigate for %s attempt %d", resolved_id, next_attempt)
            else:
                lifecycle.transition(
                    db,
                    incident,
                    lifecycle.ESCALATED,
                    reason=f"re-validation failed ({payload.result}) and {max_attempts_env} attempts are used up",
                    actor="jenkins",
                    attempt=resolved_attempt,
                )
                await db.commit()
                await db.refresh(incident)
                logger.info(
                    "Incident %s reached max attempts (%d); marked ESCALATED",
                    resolved_id,
                    max_attempts_env,
                )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "validation_received",
                "incident_id": resolved_id,
                "result": payload.result,
                "attempt": resolved_attempt,
            },
        )
