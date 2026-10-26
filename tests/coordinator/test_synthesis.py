"""Tests for Coordinator synthesis (Week 4).

**Owner:** P3. Fusion is where the three independent opinions meet, so the cases that
matter are the ones where they do not simply agree: one agent alone, agents in
conflict, everyone abstaining. A synthesis that turns a disagreement into a confident
verdict is the multi-agent version of blaming an innocent commit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from common.models import Evidence, EvidenceHypothesis, EvidenceItem, FailureTaxonomy
from coordinator.synthesis import (
	AGREEMENT_BONUS,
	CONFLICT_PENALTY,
	MAX_FUSED_CONFIDENCE,
	MAX_UNKNOWN_CONFIDENCE,
	MIN_REPORTABLE_CONFIDENCE,
	synthesize,
)


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")
NOW = datetime(2026, 10, 25, 9, 30, tzinfo=timezone.utc)

DEP = FailureTaxonomy.DEPENDENCY_REGRESSION
CFG = FailureTaxonomy.CONFIG_ERROR
UNKNOWN = FailureTaxonomy.UNKNOWN


def ev(
	agent: str,
	failure_type: FailureTaxonomy = DEP,
	confidence: float = 0.8,
	*,
	status: str = "completed",
	steps: list[str] | None = None,
	incident_id: UUID = INCIDENT,
	with_hypothesis: bool = False,
) -> Evidence:
	items: list[EvidenceItem] = []
	hypotheses: list[EvidenceHypothesis] = []
	if with_hypothesis:
		items = [EvidenceItem(id="file-1", kind="commit", source="x", content="y")]
		hypotheses = [
			EvidenceHypothesis(
				hypothesis=f"{agent} thinks {failure_type.value}",
				failure_type=failure_type,
				confidence=confidence,
				supporting_evidence=["file-1"],
			)
		]
	return Evidence(
		incident_id=incident_id,
		agent=agent,  # type: ignore[arg-type]
		created_at=NOW,
		status=status,  # type: ignore[arg-type]
		failure_type=failure_type,
		summary=f"{agent} summary",
		confidence=confidence,
		evidence_items=items,
		root_cause_hypotheses=hypotheses,
		recommended_next_steps=steps or [],
		redaction_applied=True,
	)


# --- agreement ----------------------------------------------------------------


def test_one_agent_naming_a_cause_is_reported_with_its_own_confidence() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.9), ev("metrics_agent", UNKNOWN, 0.2, status="insufficient_evidence")],
		now=NOW,
		expected_agents=("code_agent", "metrics_agent"),
	)

	assert report.status == "resolved_cause"
	assert report.failure_type is DEP
	assert report.confidence == pytest.approx(0.9)
	assert report.agreeing_agents == ("code_agent",)
	assert report.actionable is True


def test_two_agents_agreeing_raises_confidence() -> None:
	alone = synthesize([ev("code_agent", DEP, 0.7)], now=NOW)
	together = synthesize([ev("code_agent", DEP, 0.7), ev("jenkins_agent", DEP, 0.65)], now=NOW)

	assert together.confidence == pytest.approx(alone.confidence + AGREEMENT_BONUS)
	assert together.agreeing_agents == ("code_agent", "jenkins_agent")


def test_three_agents_agreeing_is_the_strongest_case() -> None:
	report = synthesize([ev(a, DEP, 0.7) for a in ("code_agent", "jenkins_agent", "metrics_agent")], now=NOW)

	assert report.confidence == pytest.approx(0.7 + 2 * AGREEMENT_BONUS)
	assert len(report.agreeing_agents) == 3


def test_agreement_never_claims_certainty() -> None:
	"""Agents can share a blind spot, so even unanimous agreement stops short of 1.0."""
	report = synthesize([ev(a, DEP, 0.98) for a in ("code_agent", "jenkins_agent", "metrics_agent")], now=NOW)

	assert report.confidence == pytest.approx(MAX_FUSED_CONFIDENCE)


def test_a_single_very_sure_agent_is_also_capped() -> None:
	"""The ceiling is the system's, not just agreement's."""
	report = synthesize([ev("metrics_agent", FailureTaxonomy.TIMEOUT, 0.97)], now=NOW)

	assert report.confidence == pytest.approx(MAX_FUSED_CONFIDENCE)


# --- disagreement -------------------------------------------------------------


def test_two_agents_disagreeing_one_each_is_conflicting_not_resolved() -> None:
	"""One vote each is a tie. Higher confidence must not be allowed to break it."""
	report = synthesize([ev("code_agent", DEP, 0.9), ev("jenkins_agent", CFG, 0.85)], now=NOW)

	assert report.status == "conflicting"
	assert report.actionable is False
	assert len(report.conflicts) == 1


def test_a_conflict_costs_confidence() -> None:
	clean = synthesize([ev("code_agent", DEP, 0.9)], now=NOW)
	contested = synthesize([ev("code_agent", DEP, 0.9), ev("jenkins_agent", CFG, 0.6)], now=NOW)

	assert contested.confidence == pytest.approx(clean.confidence - CONFLICT_PENALTY)


def test_two_against_one_resolves_toward_the_majority_and_records_the_dissent() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.8), ev("jenkins_agent", DEP, 0.7), ev("metrics_agent", CFG, 0.9)],
		now=NOW,
	)

	assert report.status == "resolved_cause"
	assert report.failure_type is DEP
	assert report.conflicts[0].failure_type is CFG
	assert report.conflicts[0].claimed_by == ("metrics_agent",)
	assert "Disagreement" in report.summary


def test_the_majority_beats_a_single_more_confident_dissenter() -> None:
	"""Two independent agents agreeing outranks one agent being very sure."""
	report = synthesize(
		[ev("code_agent", DEP, 0.6), ev("jenkins_agent", DEP, 0.6), ev("metrics_agent", CFG, 0.95)],
		now=NOW,
	)

	assert report.failure_type is DEP


# --- restraint ----------------------------------------------------------------


def test_everyone_abstaining_names_no_cause() -> None:
	report = synthesize(
		[
			ev("code_agent", UNKNOWN, 0.3, status="insufficient_evidence"),
			ev("metrics_agent", UNKNOWN, 0.2, status="insufficient_evidence"),
		],
		now=NOW,
	)

	assert report.status == "insufficient_evidence"
	assert report.failure_type is UNKNOWN
	assert report.actionable is False
	assert report.confidence <= MAX_UNKNOWN_CONFIDENCE


def test_a_failed_agent_counts_as_abstaining_not_as_a_vote() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.9), ev("jenkins_agent", DEP, 0.0, status="failed")], now=NOW
	)

	assert report.agreeing_agents == ("code_agent",)
	assert report.abstaining_agents == ("jenkins_agent",)
	assert report.confidence == pytest.approx(0.9)  # no agreement bonus from a crash


def test_abstention_is_reported_as_a_finding() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.9), ev("metrics_agent", UNKNOWN, 0.2, status="insufficient_evidence")],
		now=NOW,
	)

	assert "metrics_agent found nothing" in report.summary


def test_a_weak_lone_cause_is_not_reported() -> None:
	report = synthesize([ev("code_agent", DEP, 0.45)], now=NOW)

	assert report.status == "insufficient_evidence"
	assert report.failure_type is UNKNOWN
	assert report.actionable is False


def test_a_contested_cause_exactly_at_the_floor_is_still_reported() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.55), ev("jenkins_agent", DEP, 0.5), ev("metrics_agent", CFG, 0.5)],
		now=NOW,
	)

	# 0.55 + 0.1 agreement - 0.15 conflict = 0.50, exactly at the floor.
	assert report.confidence == pytest.approx(0.5)
	assert report.status == "resolved_cause"


def test_a_conflict_that_drags_confidence_below_the_floor_withholds_the_cause() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.5), ev("jenkins_agent", DEP, 0.5), ev("metrics_agent", CFG, 0.5)],
		now=NOW,
	)

	# 0.50 + 0.1 agreement - 0.15 conflict = 0.45: a majority, but not a confident one.
	assert report.status == "insufficient_evidence"
	assert report.failure_type is UNKNOWN
	assert report.actionable is False


def test_the_floor_matches_the_policy_gate() -> None:
	from pathlib import Path

	rego = (Path(__file__).resolve().parents[2] / "policies" / "remediation.rego").read_text(encoding="utf-8")

	assert f"min_confidence := {MIN_REPORTABLE_CONFIDENCE}" in rego


# --- provenance ---------------------------------------------------------------


def test_cited_evidence_is_namespaced_by_agent() -> None:
	"""Each agent numbers its own evidence; `file-1` is ambiguous once they are merged."""
	report = synthesize(
		[ev("code_agent", DEP, 0.9, with_hypothesis=True), ev("jenkins_agent", DEP, 0.7, with_hypothesis=True)],
		now=NOW,
	)
	cited = {item for h in report.hypotheses for item in h.supporting_evidence}

	assert cited == {"code_agent:file-1", "jenkins_agent:file-1"}


def test_hypotheses_from_every_agent_are_kept_and_ranked() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.9, with_hypothesis=True), ev("jenkins_agent", CFG, 0.4, with_hypothesis=True)],
		now=NOW,
	)

	assert [h.confidence for h in report.hypotheses] == [0.9, 0.4]


def test_next_steps_come_only_from_agents_that_named_the_reported_cause() -> None:
	report = synthesize(
		[
			ev("code_agent", DEP, 0.9, steps=["Pin httpx back."]),
			ev("metrics_agent", UNKNOWN, 0.2, status="insufficient_evidence", steps=["Check CPU."]),
		],
		now=NOW,
	)

	assert report.recommended_next_steps == ("Pin httpx back.",)


def test_duplicate_next_steps_are_merged() -> None:
	report = synthesize(
		[ev("code_agent", DEP, 0.9, steps=["Pin it."]), ev("jenkins_agent", DEP, 0.7, steps=["Pin it."])],
		now=NOW,
	)

	assert report.recommended_next_steps == ("Pin it.",)


# --- guards and rendering -----------------------------------------------------


def test_no_evidence_is_an_error_not_an_empty_report() -> None:
	with pytest.raises(ValueError, match="no evidence"):
		synthesize([], now=NOW)


def test_evidence_from_two_incidents_is_refused() -> None:
	with pytest.raises(ValueError, match="2 incidents"):
		synthesize([ev("code_agent"), ev("jenkins_agent", incident_id=uuid4())], now=NOW)


def test_the_markdown_report_names_every_agent_and_the_conflict() -> None:
	report = synthesize([ev("code_agent", DEP, 0.9), ev("jenkins_agent", CFG, 0.85)], now=NOW)
	markdown = report.to_markdown()

	assert "| code_agent | dependency_regression |" in markdown
	assert "| jenkins_agent | config_error |" in markdown
	assert "Unresolved disagreement" in markdown


def test_the_report_is_valid_structured_output() -> None:
	report = synthesize([ev("code_agent", DEP, 0.9)], now=NOW)

	assert type(report).model_validate_json(report.model_dump_json()) == report


# --- duplicate linking (TC-19) --------------------------------------------------


def _with_links(evidence: Evidence, related: list[str], count: int) -> Evidence:
	return evidence.model_copy(update={"related_incident_ids": related, "recurrence_count": count})


def test_a_recurrence_is_shown_in_the_report() -> None:
	report = synthesize(
		[_with_links(ev("jenkins_agent", DEP, 0.7), ["inc-a", "inc-b"], 2), ev("code_agent", DEP, 0.9)],
		now=NOW,
	)

	assert report.is_recurrence
	assert report.related_incident_ids == ("inc-a", "inc-b")
	assert report.recurrence_count == 2
	assert "Recurrence: seen 2 time(s) before" in report.summary
	assert "## Recurrence" in report.to_markdown() and "`inc-a`" in report.to_markdown()


def test_links_from_several_agents_are_merged_without_duplicates() -> None:
	report = synthesize(
		[
			_with_links(ev("jenkins_agent", DEP, 0.7), ["inc-a", "inc-b"], 2),
			_with_links(ev("code_agent", DEP, 0.9), ["inc-b", "inc-c"], 1),
		],
		now=NOW,
	)

	assert set(report.related_incident_ids) == {"inc-a", "inc-b", "inc-c"}
	assert len(report.related_incident_ids) == 3  # inc-b reported twice, listed once
	assert report.recurrence_count == 3  # never fewer than the distinct incidents linked


def test_an_incident_never_links_to_itself() -> None:
	report = synthesize([_with_links(ev("jenkins_agent", DEP, 0.8), [str(INCIDENT), "inc-a"], 1)], now=NOW)

	assert report.related_incident_ids == ("inc-a",)


def test_a_first_occurrence_has_no_recurrence_section() -> None:
	report = synthesize([ev("code_agent", DEP, 0.9)], now=NOW)

	assert not report.is_recurrence
	assert report.recurrence_count == 0
	assert "## Recurrence" not in report.to_markdown()
	assert "Recurrence" not in report.summary


def test_recurrence_survives_an_insufficient_evidence_report() -> None:
	"""A repeat of an unexplained failure is still worth knowing about."""
	report = synthesize(
		[_with_links(ev("jenkins_agent", UNKNOWN, 0.2, status="insufficient_evidence"), ["inc-a"], 4)], now=NOW
	)

	assert report.status == "insufficient_evidence"
	assert report.related_incident_ids == ("inc-a",)
	assert report.recurrence_count == 4


def test_a_count_without_ids_is_still_a_recurrence() -> None:
	report = synthesize([_with_links(ev("jenkins_agent", DEP, 0.8), [], 3)], now=NOW)

	assert report.is_recurrence and report.recurrence_count == 3
