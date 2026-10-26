"""The audit-completeness rules, run against hand-built trails."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from common.audit import AuditRecord
from common.audit_check import check_incident

INCIDENT = UUID("11111111-1111-4111-8111-111111111111")
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(hours=1)  # "now" for checks: older runs count as abandoned


def rec(seconds: int, actor: str, event: str, **payload) -> AuditRecord:
    return AuditRecord(
        incident_id=INCIDENT, actor=actor, event_type=event, payload=payload, created_at=T0 + timedelta(seconds=seconds)
    )


def kinds(records, **kwargs) -> list[str]:
    return [p.kind for p in check_incident(records, now=LATER, **kwargs)]


def test_a_clean_trail_has_no_problems():
    records = [
        rec(0, "metrics_agent", "agent_started"),
        rec(5, "metrics_agent", "agent_completed"),
        rec(10, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0,
            agent_outcomes={"metrics_agent": "completed"}),
    ]
    assert kinds(records, required_agents=["metrics_agent"]) == []


def test_redelivery_gives_two_valid_runs_not_a_duplicate_error():
    records = [
        rec(0, "metrics_agent", "agent_started"), rec(5, "metrics_agent", "agent_completed"),
        rec(60, "metrics_agent", "agent_started"), rec(65, "metrics_agent", "agent_failed"),
    ]
    assert kinds(records) == []


def test_a_start_with_no_terminal_event_is_flagged_once_it_is_stale():
    records = [rec(0, "code_agent", "agent_started")]
    assert kinds(records) == ["unfinished_run"]
    assert check_incident(records, now=T0 + timedelta(seconds=30)) == []  # still running


def test_two_starts_without_a_terminal_between_them_are_flagged():
    records = [rec(0, "code_agent", "agent_started"), rec(10, "code_agent", "agent_started"), rec(20, "code_agent", "agent_completed")]
    assert kinds(records) == ["unfinished_run"]


def test_a_terminal_without_a_start_is_flagged():
    assert kinds([rec(0, "code_agent", "agent_completed")]) == ["orphan_terminal"]


def test_each_actor_is_tracked_separately():
    records = [
        rec(0, "metrics_agent", "agent_started"), rec(1, "code_agent", "agent_started"),
        rec(2, "metrics_agent", "agent_completed"), rec(3, "code_agent", "agent_completed"),
    ]
    assert kinds(records) == []


def test_a_revision_audited_twice_is_flagged():
    records = [
        rec(0, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0),
        rec(1, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0),
    ]
    assert "duplicate_revision" in kinds(records)


def test_a_synthesis_event_without_a_revision_id_is_flagged():
    assert "missing_revision_id" in kinds([rec(0, "coordinator", "synthesis_completed", attempt=0)])


def test_a_revision_before_any_initial_synthesis_is_flagged():
    assert kinds([rec(0, "coordinator", "synthesis_revised", report_revision_id="r2", attempt=0)]) == ["revision_without_initial"]


def test_an_initial_synthesis_then_a_revision_is_valid_and_a_second_initial_is_not():
    valid = [
        rec(0, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0),
        rec(9, "coordinator", "synthesis_revised", report_revision_id="r2", attempt=0),
    ]
    assert kinds(valid) == []
    twice = [*valid[:1], rec(5, "coordinator", "synthesis_completed", report_revision_id="rX", attempt=0)]
    assert kinds(twice) == ["second_initial_synthesis"]


def test_a_new_attempt_may_have_its_own_initial_synthesis():
    records = [
        rec(0, "coordinator", "synthesis_completed", report_revision_id="a", attempt=0),
        rec(9, "coordinator", "synthesis_completed", report_revision_id="b", attempt=1),
    ]
    assert kinds(records) == []


def test_an_agent_reported_completed_but_never_audited_is_flagged_unless_allowed():
    records = [
        rec(0, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0,
            agent_outcomes={"jenkins_agent": "completed", "metrics_agent": "timed_out"}),
    ]
    assert kinds(records, required_agents=["jenkins_agent", "metrics_agent"]) == ["unaudited_agent"]
    assert kinds(records, required_agents=["jenkins_agent", "metrics_agent"], unaudited_ok=["jenkins_agent"]) == []


def test_a_timed_out_agent_needs_no_run_in_the_trail():
    records = [rec(0, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0, agent_outcomes={"code_agent": "timed_out"})]
    assert kinds(records, required_agents=["code_agent"]) == []


def test_unredacted_text_in_a_payload_is_flagged():
    records = [rec(0, "metrics_agent", "tool_call")]
    records[0].payload["note"] = "password=hunter2hunter2"  # bypasses the model's own validators on purpose
    assert kinds(records) == ["unredacted"]
