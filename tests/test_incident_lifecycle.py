"""Unit tests for the lifecycle helper that writes the audit row with every status change."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from backend import incident_lifecycle as lifecycle
from backend.db.audit_models import AuditEvent, AuditEventType
from backend.db.models import Incident


def make_incident(status="OPEN", attempt=0):
    return Incident(
        id="11111111-1111-4111-8111-111111111111",
        job_name="target-app/main",
        build_number=7,
        branch="main",
        failed_stage="Test",
        status=status,
        event_key="k",
        remediation_attempt=attempt,
        related_incident_id=None,
        created_at=datetime.now(timezone.utc),
    )


def staged(db) -> list[AuditEvent]:
    return [call.args[0] for call in db.add.call_args_list]


def test_creation_is_audited_with_the_job_and_attempt():
    db = MagicMock()
    lifecycle.record_created(db, make_incident(attempt=2))
    [event] = staged(db)
    assert event.event_type == AuditEventType.INCIDENT_CREATED.value
    assert event.incident_id == "11111111-1111-4111-8111-111111111111"
    assert event.payload["job_name"] == "target-app/main" and event.payload["attempt"] == 2
    assert event.summary == "Incident opened for target-app/main #7"


def test_a_transition_sets_the_status_and_stages_one_audit_row():
    db = MagicMock()
    incident = make_incident()
    lifecycle.transition(db, incident, lifecycle.ESCALATED, reason="attempts used up", actor="jenkins", attempt=3)
    assert incident.status == "ESCALATED"
    [event] = staged(db)
    assert event.event_type == AuditEventType.INCIDENT_STATUS_CHANGED.value
    assert event.payload == {"from": "OPEN", "to": "ESCALATED", "reason": "attempts used up", "attempt": 3}
    assert event.summary == "OPEN -> ESCALATED: attempts used up" and event.actor == "jenkins"


def test_the_attempt_defaults_to_the_incidents_current_attempt():
    db = MagicMock()
    lifecycle.transition(db, make_incident(attempt=4), lifecycle.RESOLVED, reason="ok", actor="jenkins")
    assert staged(db)[0].payload["attempt"] == 4


def test_terminal_statuses_are_the_three_ways_an_incident_ends():
    assert lifecycle.TERMINAL == {"RESOLVED", "ESCALATED", "CLOSED_REJECTED"}
    assert "OPEN" not in lifecycle.TERMINAL
