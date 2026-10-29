"""A rejected fix closes the incident, and every status change is on the audit trail."""

from sqlalchemy.orm import Session

from backend.db.audit_models import AuditEvent
from backend.db.models import Incident
from tests.ui.test_incidents import setup_project


def events(database, incident_id, event_type="incident_status_changed"):
    with Session(database) as session:
        return [e for e in session.query(AuditEvent).filter(AuditEvent.incident_id == incident_id, AuditEvent.event_type == event_type)]


def status_of(database, incident_id):
    with Session(database) as session:
        return session.get(Incident, incident_id).status


def test_rejecting_a_fix_closes_the_incident_and_audits_why(sign_in, world, database):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    response = sign_in("rev@acme.io").post(f"/api/v1/incidents/{incident}/decision", json={"decision": "rejected", "comment": "wrong cause"})
    assert response.status_code == 201
    assert status_of(database, incident) == "CLOSED_REJECTED"
    [event] = events(database, incident)
    assert (event.payload["from"], event.payload["to"]) == ("OPEN", "CLOSED_REJECTED")
    assert "rev@acme.io" in event.payload["reason"] and event.actor == "approval_service"


def test_approving_a_fix_leaves_the_status_to_re_validation(sign_in, world, database):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    assert sign_in("rev@acme.io").post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved"}).status_code == 201
    assert status_of(database, incident) == "OPEN"
    assert events(database, incident) == []


def test_a_rejection_does_not_reopen_or_rewrite_a_finished_incident(sign_in, world, database):
    setup_project(world)
    incident = world.incident("target-app/main", status="ESCALATED")
    world.proposal(incident)
    assert sign_in("rev@acme.io").post(f"/api/v1/incidents/{incident}/decision", json={"decision": "rejected", "comment": "no"}).status_code == 201
    assert status_of(database, incident) == "ESCALATED"
    assert events(database, incident) == []


def test_a_closed_incident_no_longer_counts_as_open(sign_in, world):
    _, project = setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    rev = sign_in("rev@acme.io")
    assert rev.get(f"/api/v1/projects/{project}").json()["open_incidents"] == 1
    rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "rejected", "comment": "no"})
    assert rev.get(f"/api/v1/projects/{project}").json()["open_incidents"] == 0


def test_the_closing_status_change_is_recorded_after_the_decision_that_caused_it(sign_in, world, database):
    """The audit checker reads the trail in time order: a close dated before its rejection would be flagged."""
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    sign_in("rev@acme.io").post(f"/api/v1/incidents/{incident}/decision", json={"decision": "rejected", "comment": "wrong cause"})
    with Session(database) as session:
        rows = session.query(AuditEvent).filter(AuditEvent.incident_id == incident).order_by(AuditEvent.created_at, AuditEvent.id).all()
    assert [r.event_type for r in rows if r.event_type in ("approval_decided", "incident_status_changed")] == ["approval_decided", "incident_status_changed"]
