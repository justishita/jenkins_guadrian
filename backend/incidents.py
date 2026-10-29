"""Shared incident idempotency and persistence for webhooks and catch-up."""

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import incident_lifecycle as lifecycle
from backend.db.models import Incident
from common.fingerprint import compute_preliminary_fingerprint
from backend.events.publisher import publisher
from backend.models.events import JenkinsFailureEvent
from common.blackboard import BlackboardEntry, get_blackboard
from common.redaction import redact_value

logger = logging.getLogger(__name__)

INCIDENT_CREATION_LOCK = asyncio.Lock()


def incident_event_key(payload: JenkinsFailureEvent) -> str:
    key_material = f"{payload.job_name}|{payload.build_number}|{payload.build_url}"
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()


async def create_incident_if_missing(
    payload: JenkinsFailureEvent,
    db: AsyncSession,
) -> tuple[Incident, bool]:
    """Persist an incident idempotently and ensure its event is delivered."""
    async with INCIDENT_CREATION_LOCK:
        payload = JenkinsFailureEvent.model_validate(
            redact_value(payload.model_dump(mode="json"))
        )
        event_key = incident_event_key(payload)
        result = await db.execute(select(Incident).where(Incident.event_key == event_key))
        existing = result.scalar_one_or_none()
        created = existing is None
        if existing is None:
            # Duplicate detection: link to prior incident matching preliminary fingerprint (job_name + failed_stage)
            related_id = None
            if payload.failed_stage:
                dup_result = await db.execute(
                    select(Incident.id)
                    .where(
                        Incident.job_name == payload.job_name,
                        Incident.failed_stage == payload.failed_stage,
                    )
                    .order_by(Incident.created_at.desc())
                    .limit(1)
                )
                related_id = dup_result.scalar_one_or_none()

            incident = Incident(
                id=payload.incident_id or str(uuid.uuid4()),
                job_name=payload.job_name,
                build_number=payload.build_number,
                build_url=payload.build_url,
                branch=payload.branch,
                git_commit=payload.git_commit,
                failed_stage=payload.failed_stage,
                status="OPEN",
                event_key=event_key,
                remediation_attempt=payload.remediation_attempt,
                related_incident_id=related_id,
            )
            db.add(incident)
            lifecycle.record_created(db, incident)
            await db.commit()
            await db.refresh(incident)
        else:
            incident = existing

        event_message = {
            "incident_id": incident.id,
            "job_name": incident.job_name,
            "build_number": incident.build_number,
            "build_url": incident.build_url,
            "branch": incident.branch,
            "git_commit": incident.git_commit,
            "failed_stage": incident.failed_stage,
            "remediation_attempt": incident.remediation_attempt,
            "related_incident_id": incident.related_incident_id,
            "preliminary_fingerprint": compute_preliminary_fingerprint(incident.job_name, incident.failed_stage),
            "timestamp": payload.timestamp.isoformat(),
            "received_at": (
                incident.created_at.astimezone(timezone.utc).isoformat()
                if incident.created_at is not None and incident.created_at.tzinfo is not None
                else payload.timestamp.isoformat()
            ),
        }

        get_blackboard().post(
            BlackboardEntry(
                incident_id=uuid.UUID(str(incident.id)),
                section="context",
                key="incident_event",
                author="backend",
                payload=event_message,
            )
        )

        published = await publisher.publish_incident_created(event_message)
        if not published:
            logger.error("Failed to dispatch incident event %s", incident.id)
            raise RuntimeError("Incident event dispatch failed")
        return incident, created
