"""Incidents, reports, reviews and decisions: what a user sees, and what they must never see."""

from uuid import uuid4

from sqlalchemy.orm import Session

from backend.db.audit_models import Approval, AuditEvent, ProposalStatus, RemediationProposal
from backend.db.ui_models import User
from backend.ui import reports


def setup_project(world, member_role="reviewer", job="target-app"):
    member = world.user("rev@acme.io", member_role)
    return member, world.project("Shop", job, [member])


def test_the_incident_list_shows_each_incident_with_its_report_and_fix_state(sign_in, world):
    _, project = setup_project(world)
    with_report = world.incident("target-app/main", age_hours=2)
    world.report(with_report)
    world.proposal(with_report)
    world.incident("target-app/main", age_hours=1)  # no report yet
    body = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/incidents").json()
    assert body["total"] == 2
    newest, older = body["items"]
    assert newest["report"] is None and newest["proposal_status"] is None
    assert older["report"]["verdict"] == "dependency_regression" and older["report"]["actionable"] is True
    assert older["proposal_status"] == ProposalStatus.AWAITING_APPROVAL.value


def test_the_incident_list_filters_and_paginates(sign_in, world):
    _, project = setup_project(world)
    for index in range(5):
        incident = world.incident("target-app/main", status="OPEN" if index % 2 == 0 else "RESOLVED", age_hours=index + 1)
        world.report(incident, verdict="timeout" if index < 2 else "config_error")
    rev = sign_in("rev@acme.io")
    assert rev.get(f"/api/v1/projects/{project}/incidents?status=resolved").json()["total"] == 2
    assert rev.get(f"/api/v1/projects/{project}/incidents?verdict=timeout").json()["total"] == 2
    page = rev.get(f"/api/v1/projects/{project}/incidents?limit=2&offset=2").json()
    assert page["total"] == 5 and len(page["items"]) == 2
    assert rev.get(f"/api/v1/projects/{project}/incidents?limit=1000").status_code == 422


def test_incident_detail_brings_everything_for_one_incident(sign_in, world, blackboard):
    _, project = setup_project(world)
    incident = world.incident("target-app/main")
    world.report(incident)
    world.proposal(incident)
    world.audit(incident, "agent_dispatch", payload={"agent": "jenkins_agent", "outcome": "completed"}, duration_ms=1200, ok=True)
    from common.blackboard import BlackboardEntry

    blackboard.post(
        BlackboardEntry(
            incident_id=incident, section="evidence", key="jenkins_agent", author="jenkins_agent",
            payload={"agent": "jenkins_agent", "status": "completed", "failure_type": "dependency_regression", "confidence": 0.9, "summary": "pin moved",
                     "evidence_items": [{"id": "log-1", "kind": "log_excerpt", "source": "console", "content": "x" * 5000}]},
        )
    )
    detail = sign_in("rev@acme.io").get(f"/api/v1/incidents/{incident}").json()
    assert detail["project"]["name"] == "Shop"
    assert detail["report"]["report"]["failure_type"] == "dependency_regression" and detail["report"]["report"]["actionable"] is True
    assert [r["revision"] for r in detail["revisions"]] == [1]
    item = detail["evidence"]["jenkins_agent"]["evidence_items"][0]
    assert len(item["content"]) <= 1501 and item["content"].endswith("…")  # long log bodies are cut
    assert detail["fix"]["branch"].startswith("ai-fix/") and detail["policy_decisions"][0]["allowed"] is True
    assert detail["awaiting_decision"] is True and detail["can_decide"] is True
    assert [e["event_type"] for e in detail["timeline"]] == ["agent_dispatch"]
    assert detail["links"]["jenkins_build"].startswith("http://jenkins/") and f"var-incident={incident}" in detail["links"]["grafana"]


def test_a_viewer_sees_the_fix_but_cannot_decide(sign_in, world):
    _, project = setup_project(world, member_role="viewer")
    incident = world.incident("target-app/main")
    world.proposal(incident)
    detail = sign_in("rev@acme.io").get(f"/api/v1/incidents/{incident}").json()
    assert detail["awaiting_decision"] is True and detail["can_decide"] is False


def test_the_timeline_never_includes_system_events(sign_in, world):
    from backend.ui.deps import SYSTEM_INCIDENT

    _, _ = setup_project(world)
    incident = world.incident("target-app/main")
    world.audit(SYSTEM_INCIDENT, "user_login", actor="rev@acme.io")
    world.audit(incident, "synthesis_completed")
    events = sign_in("rev@acme.io").get(f"/api/v1/incidents/{incident}").json()["timeline"]
    assert [e["event_type"] for e in events] == ["synthesis_completed"]


# ---- isolation: the part that must never regress -------------------------------------------------


def test_a_user_cannot_open_an_incident_of_a_project_they_are_not_in(sign_in, world):
    owner = world.user("owner@acme.io", "reviewer")
    world.user("other@acme.io", "reviewer")
    world.project("Shop", "target-app", [owner])
    incident = world.incident("target-app/main")
    world.report(incident)
    other = sign_in("other@acme.io")
    for path in (f"/api/v1/incidents/{incident}", f"/api/v1/incidents/{incident}/report", f"/api/v1/incidents/{incident}/report?format=md"):
        assert other.get(path).status_code == 404, path
    assert other.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved"}).status_code == 404


def test_an_unknown_incident_and_an_unassigned_one_look_the_same_to_non_admins(sign_in, world):
    world.user("rev@acme.io", "reviewer")
    world.project("Shop", "target-app", [world.user("m@acme.io", "reviewer")])
    unassigned = world.incident("some-other-job/main")
    rev = sign_in("rev@acme.io")
    assert rev.get(f"/api/v1/incidents/{unassigned}").status_code == 404
    assert rev.get(f"/api/v1/incidents/{uuid4()}").status_code == 404


def test_an_admin_can_open_an_unassigned_incident(sign_in, world):
    world.user("admin@acme.io", "admin")
    incident = world.incident("some-other-job/main")
    detail = sign_in("admin@acme.io").get(f"/api/v1/incidents/{incident}")
    assert detail.status_code == 200 and detail.json()["project"] is None


def test_a_malformed_incident_id_is_a_validation_error_not_a_path(sign_in, world):
    world.user("rev@acme.io", "reviewer")
    assert sign_in("rev@acme.io").get("/api/v1/incidents/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)
    assert sign_in("rev@acme.io").get("/api/v1/incidents/not-a-uuid").status_code == 422


def test_every_read_route_requires_a_session(app_client):
    for path in ("/api/v1/projects", "/api/v1/users", f"/api/v1/incidents/{uuid4()}", "/api/v1/projects/1/stats", "/api/v1/projects/1/incidents"):
        assert app_client.get(path).status_code == 401, path


# ---- reports -------------------------------------------------------------------------------------


def test_the_report_can_be_read_as_json_or_markdown(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.report(incident)
    rev = sign_in("rev@acme.io")
    as_json = rev.get(f"/api/v1/incidents/{incident}/report").json()
    assert as_json["revision"] == 1 and as_json["report"]["complete"] is True
    markdown = rev.get(f"/api/v1/incidents/{incident}/report?format=md")
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "**Cause:** dependency_regression" in markdown.text and "| jenkins_agent |" in markdown.text
    assert rev.get(f"/api/v1/incidents/{incident}/report?format=xml").status_code == 422


def test_an_incident_without_a_report_says_so(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    assert sign_in("rev@acme.io").get(f"/api/v1/incidents/{incident}/report").status_code == 404


def test_the_latest_revision_wins_and_all_are_listed(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.report(incident, missing=["code_agent"], revision=1)
    world.report(incident, revision=2)
    detail = sign_in("rev@acme.io").get(f"/api/v1/incidents/{incident}").json()
    assert detail["report"]["revision"] == 2 and [r["revision"] for r in detail["revisions"]] == [1, 2]
    assert [r["complete"] for r in detail["revisions"]] == [False, True]


def test_complete_and_actionable_follow_the_coordinators_rules():
    base = {"status": "resolved_cause", "failure_type": "timeout", "confidence": 0.8, "findings": [], "missing_agents": [], "agent_outcomes": {}}
    assert reports.derive(base) == {"complete": True, "actionable": True, "failed_agents": []}
    assert reports.derive({**base, "missing_agents": ["code_agent"]})["actionable"] is False
    assert reports.derive({**base, "agent_outcomes": {"metrics_agent": "timed_out"}})["complete"] is False
    assert reports.derive({**base, "findings": [{"agent": "code_agent", "status": "failed"}]})["failed_agents"] == ["code_agent"]
    assert reports.derive({**base, "confidence": 0.49})["actionable"] is False
    assert reports.derive({**base, "failure_type": "unknown"})["actionable"] is False
    assert reports.derive({**base, "status": "conflicting"})["actionable"] is False


def test_the_ui_rules_match_the_coordinators_implementation():
    from datetime import datetime, timezone

    from common.models import Evidence, FailureTaxonomy
    from coordinator.synthesis import MIN_REPORTABLE_CONFIDENCE, synthesize

    assert reports.MIN_REPORTABLE_CONFIDENCE == MIN_REPORTABLE_CONFIDENCE
    incident = uuid4()

    def evidence(agent, failure_type, confidence):
        return Evidence(incident_id=incident, agent=agent, created_at=datetime.now(timezone.utc), status="completed", failure_type=failure_type, summary="s", confidence=confidence, redaction_applied=True)

    cases = [
        ([evidence(a, FailureTaxonomy.TIMEOUT, 0.8) for a in ("jenkins_agent", "metrics_agent", "code_agent")], None),
        ([evidence("jenkins_agent", FailureTaxonomy.TIMEOUT, 0.8)], None),
        ([evidence(a, FailureTaxonomy.TIMEOUT, 0.3) for a in ("jenkins_agent", "metrics_agent", "code_agent")], None),
        ([evidence(a, FailureTaxonomy.TIMEOUT, 0.8) for a in ("jenkins_agent", "metrics_agent", "code_agent")], {"code_agent": "timed_out"}),
    ]
    for documents, outcomes in cases:
        report = synthesize(documents, outcomes=outcomes)
        derived = reports.derive(report.model_dump(mode="json"))
        assert (derived["complete"], derived["actionable"]) == (report.complete, report.actionable)


def test_the_incident_id_in_a_report_path_cannot_escape_the_reports_directory(world):
    import pytest

    with pytest.raises(ValueError):
        reports.revisions("../../etc")


# ---- reviews and decisions -----------------------------------------------------------------------


def test_the_review_queue_lists_only_undecided_fixes_of_the_project(sign_in, world):
    _, project = setup_project(world)
    waiting = world.incident("target-app/main")
    world.proposal(waiting)
    world.report(waiting)
    decided = world.incident("target-app/main")
    world.proposal(decided, status=ProposalStatus.APPROVED.value)
    other_project = world.incident("elsewhere/main")
    world.proposal(other_project)
    queue = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/reviews").json()
    assert [q["incident"]["id"] for q in queue] == [waiting]
    assert queue[0]["fix"]["failure_type"] == "dependency_regression" and queue[0]["report"]["verdict"] == "dependency_regression"


def test_a_reviewer_approves_a_fix_as_themselves(sign_in, world, database, published):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    response = sign_in("rev@acme.io").post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved", "comment": "looks right"})
    assert response.status_code == 201, response.text
    assert response.json()["approval"]["reviewer"] == "rev@acme.io"  # the signed-in user, not a typed name
    with Session(database) as session:
        approval = session.query(Approval).one()
        proposal = session.query(RemediationProposal).one()
        event = session.query(AuditEvent).filter(AuditEvent.event_type == "approval_decided").one()
    assert (approval.decision, proposal.status) == ("approved", ProposalStatus.APPROVED.value)
    assert event.payload["reviewer"] == "rev@acme.io" and event.payload["decision"] == "approved"
    published.assert_awaited_once()


def test_a_viewer_cannot_decide(sign_in, world):
    setup_project(world, member_role="viewer")
    incident = world.incident("target-app/main")
    world.proposal(incident)
    assert sign_in("rev@acme.io").post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved"}).status_code == 403


def test_a_decision_needs_the_csrf_header(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    rev = sign_in("rev@acme.io")
    assert rev.client.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved"}).status_code == 403


def test_rejecting_needs_a_reason(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    rev = sign_in("rev@acme.io")
    assert rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "rejected", "comment": "  "}).status_code == 422
    assert rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "rejected", "comment": "wrong cause"}).status_code == 201


def test_a_fix_can_only_be_decided_once(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    rev = sign_in("rev@acme.io")
    assert rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved"}).status_code == 201
    again = rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "rejected", "comment": "changed my mind"})
    assert again.status_code == 409


def test_a_policy_blocked_fix_cannot_be_approved_and_only_an_admin_may_override(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident, allowed=False)
    rev = sign_in("rev@acme.io")
    assert rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved"}).status_code == 409  # TC-15 holds in the UI too
    assert rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved", "human_override": True}).status_code == 403


def test_deciding_on_an_incident_with_no_proposal_is_a_conflict(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    assert sign_in("rev@acme.io").post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved"}).status_code == 409


def test_unknown_decision_values_and_extra_fields_are_rejected(sign_in, world):
    setup_project(world)
    incident = world.incident("target-app/main")
    world.proposal(incident)
    rev = sign_in("rev@acme.io")
    assert rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "changes_requested"}).status_code == 422
    assert rev.post(f"/api/v1/incidents/{incident}/decision", json={"decision": "approved", "reviewer": "someone-else"}).status_code == 422


def test_user_rows_never_leak_a_password_hash_through_any_response(sign_in, world):
    world.user("admin@acme.io", "admin")
    admin = sign_in("admin@acme.io")
    text = admin.get("/api/v1/users").text + admin.get("/api/v1/auth/me").text
    assert "password" not in text.lower() and "$2b$" not in text
    with Session(world.engine) as session:
        assert session.query(User).count() == 1
