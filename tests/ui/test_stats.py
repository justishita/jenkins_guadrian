"""The numbers behind the project charts."""

from backend.ui.stats import percentile


def seeded(world):
    member = world.user("rev@acme.io", "reviewer")
    project = world.project("Shop", "target-app", [member])
    first = world.incident("target-app/main", age_hours=30)
    second = world.incident("target-app/main", age_hours=3)
    world.incident("target-app-2/main")  # another project's incident: must not be counted
    world.audit(first, "synthesis_completed", payload={"verdict": "timeout", "completeness": "complete", "conflict_status": "none"}, age_hours=29.99)
    world.audit(second, "synthesis_completed", payload={"verdict": "timeout", "completeness": "incomplete", "conflict_status": "conflicted"}, age_hours=2.99)
    world.audit(first, "agent_completed", actor="metrics_agent", duration_ms=30000, ok=True)
    world.audit(second, "agent_completed", actor="metrics_agent", duration_ms=50000, ok=True)
    world.audit(first, "agent_dispatch", payload={"agent": "jenkins_agent", "outcome": "completed"}, duration_ms=20000, ok=True)
    world.audit(first, "agent_dispatch", payload={"agent": "metrics_agent", "outcome": "completed"}, duration_ms=31000, ok=True)  # has its own run
    world.audit(second, "agent_dispatch", payload={"agent": "code_agent", "outcome": "timed_out"}, duration_ms=120000, ok=False)
    world.audit(first, "approval_decided", payload={"decision": "approved"}, ok=True)
    world.audit(second, "approval_decided", payload={"decision": "rejected"}, ok=False)
    world.audit(first, "revalidation_completed", actor="jenkins", ok=True)
    world.audit(second, "revalidation_completed", actor="jenkins", ok=False)
    world.audit(second, "policy_evaluated", payload={"allowed": False}, ok=False)
    world.audit(first, "pull_request_created", ok=True)
    world.proposal(second)
    return project


def test_counts_cover_only_this_projects_incidents(sign_in, world):
    project = seeded(world)
    stats = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/stats?days=7").json()
    assert stats["incidents_total"] == 2
    assert sum(day["incidents"] for day in stats["incidents_per_day"]) == 2 and len(stats["incidents_per_day"]) == 7
    assert stats["verdicts"] == [{"verdict": "timeout", "count": 2}]
    assert (stats["reports"]["total"], stats["reports"]["incomplete"], stats["reports"]["conflicted"]) == (2, 1, 1)


def test_time_to_report_is_measured_from_the_incident(sign_in, world):
    project = seeded(world)
    reports = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/stats").json()["reports"]
    assert reports["time_to_report_p50_s"] is not None and reports["time_to_report_p50_s"] > 0


def test_agent_time_uses_own_runs_and_dispatch_for_agents_that_audit_nothing(sign_in, world):
    project = seeded(world)
    rows = {r["agent"]: r for r in sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/stats").json()["agent_time"]}
    assert rows["metrics_agent"]["runs"] == 2 and rows["metrics_agent"]["p50_s"] in (30.0, 50.0)  # not double counted via dispatch
    assert rows["jenkins_agent"]["p50_s"] == 20.0 and rows["jenkins_agent"]["runs"] == 1
    assert rows["code_agent"]["problems"] == 1 and rows["code_agent"]["p50_s"] is None  # timed out: counted, no duration


def test_approvals_and_the_accuracy_proxy(sign_in, world):
    project = seeded(world)
    stats = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/stats").json()
    assert stats["approvals"]["decided"] == 2 and stats["approvals"]["approval_rate_pct"] == 50.0 and stats["approvals"]["pending"] == 1
    outcomes = stats["fix_outcomes"]
    assert (outcomes["revalidation_passed"], outcomes["revalidation_failed"], outcomes["fix_success_pct"]) == (1, 1, 50.0)
    assert outcomes["policy_denied"] == 1 and outcomes["pull_requests_opened"] == 1
    assert "proxy" in outcomes["note"]


def test_a_project_with_no_incidents_returns_empty_but_valid_numbers(sign_in, world):
    member = world.user("rev@acme.io", "reviewer")
    project = world.project("Empty", "nothing", [member])
    stats = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/stats").json()
    assert stats["incidents_total"] == 0 and stats["verdicts"] == [] and stats["agent_time"] == []
    assert stats["approvals"]["approval_rate_pct"] is None and stats["fix_outcomes"]["fix_success_pct"] is None


def test_the_day_window_is_validated(sign_in, world):
    member = world.user("rev@acme.io", "reviewer")
    project = world.project("Shop", "target-app", [member])
    rev = sign_in("rev@acme.io")
    assert rev.get(f"/api/v1/projects/{project}/stats?days=0").status_code == 422
    assert rev.get(f"/api/v1/projects/{project}/stats?days=1000").status_code == 422


def test_percentile_picks_a_value_from_the_data():
    assert percentile([], 0.5) is None
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 100.0], 0.5) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 100.0], 0.95) == 100.0


def test_outcomes_count_how_each_incident_ended_and_time_the_resolved_ones(sign_in, world):
    member = world.user("rev@acme.io", "reviewer")
    project = world.project("Shop", "target-app", [member])
    fixed = world.incident("target-app/main", status="RESOLVED", age_hours=3)
    world.audit(fixed, "incident_status_changed", payload={"from": "OPEN", "to": "RESOLVED"}, age_hours=2)  # one hour after it opened
    rejected = world.incident("target-app/main", status="CLOSED_REJECTED", age_hours=3)
    world.audit(rejected, "approval_decided", payload={"decision": "rejected", "reviewer": "rev@acme.io"}, ok=False, age_hours=2)
    world.incident("target-app/main", status="ESCALATED", age_hours=3)
    waiting = world.incident("target-app/main", age_hours=3)
    world.proposal(waiting)
    world.incident("target-app/main", age_hours=3)
    world.incident("elsewhere/main", status="RESOLVED")  # another project: not counted
    outcomes = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/stats?days=7").json()["outcomes"]
    assert {row["outcome"]: row["count"] for row in outcomes["by_outcome"]} == {
        "resolved_by_fix": 1, "rejected": 1, "escalated": 1, "awaiting_approval": 1, "open": 1,
    }
    assert [row["outcome"] for row in outcomes["by_outcome"]] == ["resolved_by_fix", "awaiting_approval", "open", "rejected", "escalated"]
    assert 3500 < outcomes["resolved_p50_s"] < 3700  # about one hour from opening to the status change


def test_a_project_with_no_incidents_has_empty_outcomes(sign_in, world):
    member = world.user("rev@acme.io", "reviewer")
    project = world.project("Empty", "nothing", [member])
    outcomes = sign_in("rev@acme.io").get(f"/api/v1/projects/{project}/stats").json()["outcomes"]
    assert outcomes == {"by_outcome": [], "resolved_p50_s": None, "resolved_p95_s": None}
