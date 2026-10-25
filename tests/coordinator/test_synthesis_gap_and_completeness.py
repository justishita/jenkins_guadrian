"""Confidence-gap tie-breaking and completeness in Coordinator synthesis.

**Owner:** P2 (changes to P3's synthesis). Two behaviours added on top of the Week 4
fusion: a lead is only a tie when the strongest claims are close, and a report says when
an expected agent is missing or failed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from common.models import Evidence, FailureTaxonomy
from coordinator.synthesis import MIN_REPORTABLE_CONFIDENCE, TIE_MARGIN, synthesize

INCIDENT = UUID("11111111-2222-3333-4444-555555555555")
NOW = datetime(2026, 10, 25, 9, 30, tzinfo=timezone.utc)
DEP = FailureTaxonomy.DEPENDENCY_REGRESSION
CFG = FailureTaxonomy.CONFIG_ERROR
UNKNOWN = FailureTaxonomy.UNKNOWN


def ev(agent: str, failure_type: FailureTaxonomy, confidence: float, status: str = "completed") -> Evidence:
    return Evidence(
        incident_id=INCIDENT,
        agent=agent,  # type: ignore[arg-type]
        created_at=NOW,
        status=status,  # type: ignore[arg-type]
        failure_type=failure_type,
        summary=f"{agent} summary",
        confidence=confidence,
        redaction_applied=True,
    )


ALL_THREE_QUIET = [ev("metrics_agent", UNKNOWN, 0.2)]


class TestConfidenceGapTieBreak:
    def test_a_clear_lead_between_single_agent_causes_is_not_a_tie(self):
        # Jenkins 0.90 vs Code 0.55: previously both were "one agent each" and so tied.
        report = synthesize([ev("jenkins_agent", DEP, 0.9), ev("code_agent", CFG, 0.55), *ALL_THREE_QUIET], now=NOW)
        assert report.status == "resolved_cause"
        assert report.failure_type is DEP
        assert [c.failure_type for c in report.conflicts] == [CFG]  # still reported and penalised
        assert report.confidence == pytest.approx(0.75)
        assert report.actionable

    def test_close_claims_from_the_same_number_of_agents_still_tie(self):
        report = synthesize([ev("jenkins_agent", DEP, 0.80), ev("code_agent", CFG, 0.75), *ALL_THREE_QUIET], now=NOW)
        assert report.status == "conflicting" and not report.actionable

    def test_the_margin_boundary_is_inclusive(self):
        at_edge = synthesize([ev("jenkins_agent", DEP, 0.80), ev("code_agent", CFG, 0.80 - TIE_MARGIN)], now=NOW)
        beyond = synthesize([ev("jenkins_agent", DEP, 0.80), ev("code_agent", CFG, 0.80 - TIE_MARGIN - 0.05)], now=NOW)
        assert at_edge.status == "conflicting"
        assert beyond.status != "conflicting"

    def test_more_agents_still_beat_a_stronger_single_agent(self):
        report = synthesize(
            [ev("jenkins_agent", DEP, 0.6), ev("metrics_agent", DEP, 0.6), ev("code_agent", CFG, 0.95)], now=NOW
        )
        assert report.failure_type is DEP and report.agreeing_agents == ("jenkins_agent", "metrics_agent")

    def test_a_clear_lead_that_is_penalised_below_the_floor_is_still_insufficient(self):
        report = synthesize([ev("jenkins_agent", DEP, 0.60), ev("code_agent", CFG, 0.30)], now=NOW)
        assert report.confidence < MIN_REPORTABLE_CONFIDENCE
        assert report.status == "insufficient_evidence" and not report.actionable

    def test_result_does_not_depend_on_agent_order(self):
        documents = [ev("jenkins_agent", DEP, 0.9), ev("code_agent", CFG, 0.55), ev("metrics_agent", UNKNOWN, 0.2)]
        assert synthesize(documents, now=NOW) == synthesize(list(reversed(documents)), now=NOW)


class TestCompleteness:
    def test_all_three_reporting_is_complete(self):
        report = synthesize(
            [ev("jenkins_agent", DEP, 0.9), ev("code_agent", DEP, 0.7), ev("metrics_agent", UNKNOWN, 0.2)], now=NOW
        )
        assert report.complete and report.missing_agents == () and report.failed_agents == ()

    def test_a_missing_agent_makes_the_report_incomplete_but_keeps_the_verdict(self):
        report = synthesize([ev("jenkins_agent", DEP, 0.9), ev("metrics_agent", UNKNOWN, 0.2)], now=NOW)
        assert not report.complete
        assert report.missing_agents == ("code_agent",)
        assert report.failure_type is DEP and report.status == "resolved_cause"
        assert "Incomplete: no evidence from code_agent" in report.summary

    def test_a_failed_agent_makes_the_report_incomplete(self):
        report = synthesize(
            [ev("jenkins_agent", DEP, 0.9), ev("code_agent", CFG, 0.9, status="failed"), ev("metrics_agent", UNKNOWN, 0.2)],
            now=NOW,
        )
        assert report.failed_agents == ("code_agent",) and not report.complete
        assert report.missing_agents == ()

    def test_incompleteness_does_not_change_actionability(self):
        # Gating on completeness is a policy decision for the team; synthesis only reports it.
        report = synthesize([ev("jenkins_agent", DEP, 0.9)], now=NOW)
        assert not report.complete and report.actionable

    def test_nothing_named_still_lists_who_is_missing(self):
        report = synthesize([ev("metrics_agent", UNKNOWN, 0.2)], now=NOW)
        assert report.missing_agents == ("jenkins_agent", "code_agent")

    def test_expected_agents_can_be_narrowed(self):
        report = synthesize([ev("jenkins_agent", DEP, 0.9)], now=NOW, expected_agents=("jenkins_agent",))
        assert report.complete

    def test_markdown_flags_the_gap(self):
        markdown = synthesize([ev("jenkins_agent", DEP, 0.9)], now=NOW).to_markdown()
        assert "**Incomplete:** no evidence from metrics_agent, code_agent" in markdown
