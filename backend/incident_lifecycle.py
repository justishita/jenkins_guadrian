"""Incident status changes, each written to the audit trail in the same transaction.

The audit trail is where an incident's outcome is derived from (``monitoring/sql/incident_outcomes.sql``),
so every place that changes ``Incident.status`` goes through :func:`transition` instead of assigning it.
Callers commit; this function only stages the change and its audit row.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.audit_models import AuditEvent, AuditEventType
from backend.db.models import Incident

OPEN = "OPEN"
RESOLVED = "RESOLVED"
ESCALATED = "ESCALATED"
CLOSED_REJECTED = "CLOSED_REJECTED"

#: Statuses an incident does not leave on its own (no further automatic work is scheduled).
TERMINAL = frozenset({RESOLVED, ESCALATED, CLOSED_REJECTED})


def record_created(db: AsyncSession, incident: Incident) -> None:
    """Audit row for a new incident: the start of its outcome clock."""
    db.add(
        AuditEvent(
            incident_id=str(incident.id),
            actor="backend",
            event_type=AuditEventType.INCIDENT_CREATED.value,
            summary=f"Incident opened for {incident.job_name}" + (f" #{incident.build_number}" if incident.build_number else ""),
            payload={
                "job_name": incident.job_name,
                "build_number": incident.build_number,
                "branch": incident.branch,
                "failed_stage": incident.failed_stage,
                "attempt": incident.remediation_attempt,
                "related_incident_id": incident.related_incident_id,
            },
        )
    )


def transition(db: AsyncSession, incident: Incident, new_status: str, *, reason: str, actor: str, attempt: int | None = None) -> None:
    """Set ``incident.status`` and stage the audit row. ``OPEN -> OPEN`` is allowed: it records a re-investigation."""
    previous = incident.status
    incident.status = new_status
    db.add(
        AuditEvent(
            incident_id=str(incident.id),
            actor=actor,
            event_type=AuditEventType.INCIDENT_STATUS_CHANGED.value,
            summary=f"{previous} -> {new_status}: {reason}",
            payload={
                "from": previous,
                "to": new_status,
                "reason": reason,
                "attempt": attempt if attempt is not None else incident.remediation_attempt,
            },
        )
    )
