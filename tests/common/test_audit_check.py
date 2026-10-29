"""The audit-completeness rules, run against hand-built trails."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from common.audit import AuditRecord
from common.audit_check import check_incident, errors

INCIDENT = UUID("11111111-1111-4111-8111-111111111111")
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(hours=1)  # "now" for checks: older runs count as abandoned


def rec(seconds: int, actor: str, event: str, **payload) -> AuditRecord:
    return AuditRecord(
        incident_id=INCIDENT, actor=actor, event_type=event, payload=payload, created_at=T0 + timedelta(seconds=seconds)
    )


def dispatch(seconds: int, agent: str, outcome: str = "completed", attempt: int = 0, duration_ms: float | None = 1200.0) -> AuditRecord:
    return AuditRecord(
        incident_id=INCIDENT,
        actor="coordinator",
        event_type="agent_dispatch",
        payload={"agent": agent, "attempt": attempt, "outcome": outcome, "via": "a2a"},
        duration_ms=duration_ms,
        created_at=T0 + timedelta(seconds=seconds),
    )


def kinds(records, **kwargs) -> list[str]:
    """The kinds of *error* found; warnings are a separate class (see ``warnings``)."""
    return [p.kind for p in errors(check_incident(records, now=LATER, **kwargs))]


def warnings(records, **kwargs) -> list[str]:
    return [p.kind for p in check_incident(records, now=LATER, **kwargs) if p.severity == "warning"]


def test_a_clean_trail_has_no_problems():
    records = [
        rec(0, "metrics_agent", "agent_started"),
        rec(5, "metrics_agent", "agent_completed"),
        dispatch(6, "metrics_agent"),
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
        dispatch(0, "metrics_agent", "timed_out"),
        rec(1, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0, reason="recovered",
            agent_outcomes={"jenkins_agent": "completed", "metrics_agent": "timed_out"}),
    ]
    assert kinds(records, required_agents=["jenkins_agent", "metrics_agent"]) == ["unaudited_agent"]
    assert kinds(records, required_agents=["jenkins_agent", "metrics_agent"], unaudited_ok=["jenkins_agent"]) == []


def test_a_timed_out_agent_needs_no_run_in_the_trail():
    records = [
        dispatch(0, "code_agent", "timed_out"),
        rec(1, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0, agent_outcomes={"code_agent": "timed_out"}),
    ]
    assert kinds(records, required_agents=["code_agent"]) == []


def test_unredacted_text_in_a_payload_is_flagged():
    records = [rec(0, "metrics_agent", "tool_call")]
    records[0].payload["note"] = "password=hunter2hunter2"  # bypasses the model's own validators on purpose
    assert kinds(records) == ["unredacted"]


# ---- agent_dispatch --------------------------------------------------------------------


def test_a_dispatch_audits_an_agent_that_writes_no_audit_of_its_own():
    """The Jenkins agent has no agent_started events; the Coordinator's dispatch record covers it."""
    records = [
        dispatch(0, "jenkins_agent"),
        rec(5, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0, agent_outcomes={"jenkins_agent": "completed"}),
    ]
    assert kinds(records, required_agents=["jenkins_agent"]) == []


def test_a_live_report_that_lists_an_agent_which_was_never_dispatched_is_flagged():
    records = [
        dispatch(0, "metrics_agent"),
        rec(5, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0,
            agent_outcomes={"metrics_agent": "completed", "code_agent": "not_dispatched"}),
    ]
    assert kinds(records, required_agents=["metrics_agent"]) == ["undispatched_outcome"]


def test_a_recovered_report_needs_no_dispatch_records():
    records = [rec(5, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0, reason="recovered",
                   agent_outcomes={"metrics_agent": "timed_out"})]
    assert kinds(records, required_agents=["metrics_agent"]) == []


def test_dispatch_is_matched_per_attempt():
    records = [
        dispatch(0, "metrics_agent", attempt=0),
        rec(5, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0, agent_outcomes={"metrics_agent": "completed"}),
        rec(50, "coordinator", "synthesis_completed", report_revision_id="r2", attempt=1, agent_outcomes={"metrics_agent": "completed"}),
    ]
    assert kinds(records, required_agents=["metrics_agent"]) == ["undispatched_outcome"]  # attempt 1 was never dispatched


def test_an_unknown_dispatch_outcome_and_a_missing_duration_are_flagged():
    records = [dispatch(0, "code_agent", outcome="mostly_done", duration_ms=None)]
    assert sorted(kinds(records)) == ["bad_dispatch_outcome", "dispatch_without_duration"]


# ---- Week 7: the remediation lifecycle -------------------------------------------------------------------------


def status(seconds, before, after, attempt=0):
    return rec(seconds, "jenkins", "incident_status_changed", **{"from": before, "to": after, "attempt": attempt})


def decided(seconds, decision="approved", attempt=0, **extra):
    return rec(seconds, "approval_service", "approval_decided", decision=decision, attempt=attempt, **extra)


def full_flow(attempt=0, start=0):
    """A complete guarded remediation: request, policy, approval, pull request, passing re-validation, resolved."""
    return [
        rec(start, "gate", "approval_requested", attempt=attempt),
        rec(start + 1, "gate", "policy_evaluated", attempt=attempt, allowed=True),
        decided(start + 2, attempt=attempt),
        rec(start + 3, "gate", "pull_request_created", attempt=attempt),
        AuditRecord(incident_id=INCIDENT, actor="jenkins", event_type="revalidation_completed", payload={"attempt": attempt}, ok=True, created_at=T0 + timedelta(seconds=start + 4)),
        status(start + 5, "OPEN", "RESOLVED", attempt),
    ]


def test_a_complete_guarded_remediation_has_no_errors_and_no_warnings():
    records = [rec(-1, "backend", "incident_created")] + full_flow()
    assert kinds(records) == [] and warnings(records) == []


def test_a_pull_request_before_any_decision_is_an_error():
    assert "pr_without_approval" in kinds([rec(0, "gate", "pull_request_created", attempt=0)])


def test_a_pull_request_after_a_rejection_is_an_error():
    records = [decided(0, "rejected"), rec(1, "gate", "pull_request_created", attempt=0)]
    assert "pr_without_approval" in kinds(records)


def test_an_approval_over_a_policy_refusal_is_an_error_unless_a_human_overrode_it():
    refused = rec(0, "gate", "policy_evaluated", attempt=0, allowed=False)
    assert "approved_over_policy_denial" in kinds([refused, decided(1)])
    assert "approved_over_policy_denial" not in kinds([refused, decided(1, human_override=True)])


def test_a_trail_without_the_gates_events_is_a_warning_not_an_error():
    records = [decided(0), AuditRecord(incident_id=INCIDENT, actor="jenkins", event_type="revalidation_completed", payload={"attempt": 0}, ok=True, created_at=T0 + timedelta(seconds=9))]
    assert kinds(records) == []
    assert warnings(records).count("gate_not_in_live_path") == 3  # no request, no policy, no pull request


def test_a_report_without_an_incident_created_event_is_a_legacy_warning():
    records = [rec(0, "coordinator", "synthesis_completed", report_revision_id="r1", attempt=0)]
    assert "legacy_trail" in warnings(records)
    assert "legacy_trail" not in warnings([rec(-1, "backend", "incident_created")] + records)


def test_resolved_needs_a_passing_revalidation_and_escalated_a_failed_one():
    assert "resolved_without_revalidation" in kinds([status(0, "OPEN", "RESOLVED")])
    assert "escalated_without_failures" in kinds([status(0, "OPEN", "ESCALATED")])
    failed = AuditRecord(incident_id=INCIDENT, actor="jenkins", event_type="revalidation_completed", payload={"attempt": 0}, ok=False, created_at=T0)
    assert "escalated_without_failures" not in kinds([failed, status(5, "OPEN", "ESCALATED")])


def test_closed_rejected_needs_a_rejection_decision():
    assert "closed_without_rejection" in kinds([status(0, "OPEN", "CLOSED_REJECTED")])
    assert "closed_without_rejection" not in kinds([decided(0, "rejected"), status(1, "OPEN", "CLOSED_REJECTED")])


def test_the_status_chain_must_start_at_open_and_each_change_must_continue_the_last():
    assert "status_chain_broken" in kinds([status(0, "RESOLVED", "OPEN")])
    assert "status_chain_broken" in kinds([status(0, "OPEN", "OPEN", 1), status(5, "ESCALATED", "OPEN", 1)])
    assert "status_chain_broken" not in kinds([status(0, "OPEN", "OPEN", 1), status(5, "OPEN", "OPEN", 2)])


def test_nothing_changes_after_a_terminal_status():
    failed = AuditRecord(incident_id=INCIDENT, actor="jenkins", event_type="revalidation_completed", payload={"attempt": 0}, ok=False, created_at=T0)
    records = [failed, status(5, "OPEN", "ESCALATED"), status(9, "ESCALATED", "OPEN")]
    assert "reopened_after_terminal" in kinds(records)


def test_attempts_never_go_backwards():
    records = [decided(0, attempt=2), status(1, "OPEN", "OPEN", attempt=1)]
    assert "attempt_regressed" in kinds(records)
    assert "attempt_regressed" not in kinds([decided(0, attempt=1), status(1, "OPEN", "OPEN", attempt=1), decided(2, attempt=2)])
