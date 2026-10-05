"""Shared incident idempotency and persistence for webhooks and catch-up."""

import asyncio
from datetime import datetime, timezone
import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Incident
from backend.events.publisher import publisher
from backend.models.events import JenkinsFailureEvent


INCIDENT_CREATION_LOCK = asyncio.Lock()


def incident_event_key(payload: JenkinsFailureEvent) -> str:
    key_material = f"{payload.job_name}|{payload.build_number}|{payload.build_url}"
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()


async def create_incident_if_missing(
    payload: JenkinsFailureEvent,
    db: AsyncSession,
) -> tuple[Incident, bool]:
    """Serialize local incident creation and publish only once per event key."""
    async with INCIDENT_CREATION_LOCK:
        event_key = incident_event_key(payload)
        result = await db.execute(select(Incident).where(Incident.event_key == event_key))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing, False

        incident_id = payload.incident_id or str(uuid.uuid4())
        received_at = datetime.now(timezone.utc).isoformat()
        incident = Incident(
            id=incident_id,
            job_name=payload.job_name,
            build_number=payload.build_number,
            build_url=payload.build_url,
            branch=payload.branch,
            git_commit=payload.git_commit,
            failed_stage=payload.failed_stage,
            status="OPEN",
            event_key=event_key,
            remediation_attempt=payload.remediation_attempt,
        )
        db.add(incident)
        await db.commit()
        await db.refresh(incident)

        event_message = {
            "incident_id": incident_id,
            "job_name": payload.job_name,
            "build_number": payload.build_number,
            "build_url": payload.build_url,
            "branch": payload.branch,
            "git_commit": payload.git_commit,
            "failed_stage": payload.failed_stage,
            "remediation_attempt": payload.remediation_attempt,
            "timestamp": payload.timestamp.isoformat(),
            "received_at": received_at,
        }
        await publisher.publish_incident_created(event_message)
        return incident, True
