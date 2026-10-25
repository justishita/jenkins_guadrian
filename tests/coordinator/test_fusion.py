"""Decision-table and property tests for coordinator.fusion."""

from dataclasses import replace
from itertools import permutations

import pytest

from coordinator.fusion import FusionConfig, TypeRelation, fuse, noisy_or, weighted_mean
from coordinator.report import (
    BlockingCode,
    Completeness,
    ConflictStatus,
    FusionMethod,
    NegativeKind,
    NegativeStatus,
    RelationStatus,
)
from tests.coordinator.helpers import NOW, T, make, view


def run(*evidence, config=None):
    return fuse(view(*evidence), config, now=NOW)


def score_of(report, ftype):
    return next(s for s in report.ranking if s.failure_type == ftype)


class TestScoring:
    def test_worked_example_noisy_or_and_weighted_mean(self):
        # Jenkins 0.90 x 1.0 and Code 0.40 x 0.8 for the same type.
        assert noisy_or([0.9, 0.32]) == pytest.approx(0.932)
        assert weighted_mean([0.9, 0.32], [1.0, 0.8]) == pytest.approx(0.6778, abs=1e-3)

        report = run(make("jenkins_agent", T.DEPENDENCY_REGRESSION, 0.9), make("code_agent", T.DEPENDENCY_REGRESSION, 0.4))
        top = score_of(report, T.DEPENDENCY_REGRESSION)
        assert top.score == pytest.approx(0.932)
        assert top.alternative_score == pytest.approx(0.6778, abs=1e-3)

    def test_raw_confidence_relevance_and_product_are_recorded_separately(self):
        report = run(make("code_agent", T.DEPENDENCY_REGRESSION, 0.4))
        c = score_of(report, T.DEPENDENCY_REGRESSION).contributions[0]
        assert (c.confidence, c.relevance, c.weighted) == (0.4, 0.8, 0.32)
        assert report.agent_verdicts[0].confidence == 0.4  # the agent's own number is untouched

    def test_selected_method_drives_score_and_other_is_reported(self):
        evidence = [make("jenkins_agent", T.TIMEOUT, 0.9), make("metrics_agent", T.TIMEOUT, 0.8)]
        by_or = run(*evidence)
        by_mean = run(*evidence, config=FusionConfig(method=FusionMethod.WEIGHTED_MEAN))
        assert score_of(by_or, T.TIMEOUT).alternative_score == score_of(by_mean, T.TIMEOUT).score
        assert by_mean.fusion_method == FusionMethod.WEIGHTED_MEAN

    def test_corroboration_never_lowers_noisy_or_score(self):
        alone = run(make("jenkins_agent", T.TIMEOUT, 0.9))
        together = run(make("jenkins_agent", T.TIMEOUT, 0.9), make("metrics_agent", T.TIMEOUT, 0.1))
        assert score_of(together, T.TIMEOUT).score >= score_of(alone, T.TIMEOUT).score

    def test_each_agent_counts_once(self):
        # Many hypotheses of one type from one agent still make a single vote.
        ev = make("jenkins_agent", T.TIMEOUT, 0.8, contradicts={T.AUTH_FAILURE: ["a"]})
        report = run(ev)
        assert len(score_of(report, T.TIMEOUT).contributions) == 1

    def test_secondary_hypotheses_are_shown_not_scored(self):
        report = run(make("jenkins_agent", T.TIMEOUT, 0.8, contradicts={T.AUTH_FAILURE: ["a"]}))
        assert [s.failure_type for s in report.ranking] == [T.TIMEOUT]
        assert report.agent_verdicts[0].alternative_hypotheses == ["auth_failure"]


class TestDeterminism:
    EVIDENCE = (
        make("jenkins_agent", T.DEPENDENCY_REGRESSION, 0.9),
        make("code_agent", T.CONFIG_ERROR, 0.55),
        make("metrics_agent", T.UNKNOWN, 0.2, status="insufficient_evidence"),
    )

    def test_same_input_same_report(self):
        assert run(*self.EVIDENCE) == run(*self.EVIDENCE)

    def test_agent_order_does_not_matter(self):
        reports = {run(*p).model_dump_json() for p in permutations(self.EVIDENCE)}
        assert len(reports) == 1

    def test_only_generated_at_may_differ(self):
        later = fuse(view(*self.EVIDENCE), now=NOW.replace(hour=13))
        a, b = run(*self.EVIDENCE).model_dump(), later.model_dump()
        a.pop("generated_at"), b.pop("generated_at")
        assert a == b


class TestNoVoteAndTies:
    def test_all_unknown_is_unknown_with_zero_confidence_not_a_fabricated_winner(self):
        report = run(*(make(a, T.UNKNOWN, 0.2) for a in ("jenkins_agent", "metrics_agent", "code_agent")))
        assert report.verdict == T.UNKNOWN and report.confidence == 0.0
        assert report.ranking == []
        assert BlockingCode.UNKNOWN_VERDICT in {b.code for b in report.blocking_reasons}
        assert not report.ready_for_policy_review

    def test_no_evidence_at_all(self):
        report = fuse(view(), now=NOW)
        assert report.verdict == T.UNKNOWN
        assert report.completeness == Completeness.INCOMPLETE
        assert report.missing_agents == ["code_agent", "jenkins_agent", "metrics_agent"]

    def test_zero_confidence_and_out_of_domain_votes_are_ignored(self):
        config = replace(FusionConfig(), relevance={"jenkins_agent": {}, "code_agent": {T.CONFIG_ERROR: 0.8}})
        report = run(make("jenkins_agent", T.TIMEOUT, 0.9), make("code_agent", T.CONFIG_ERROR, 0.0), config=config)
        assert report.verdict == T.UNKNOWN
        notes = {v.agent: v.vote_note for v in report.agent_verdicts}
        assert notes == {"jenkins_agent": "outside the agent's domain", "code_agent": "zero confidence"}

    @pytest.mark.parametrize("order", list(permutations(["jenkins_agent", "code_agent"])))
    def test_tie_is_deterministic_regardless_of_agent_order(self, order):
        # Equal weighted contribution for two types: tie broken by name, not arrival order.
        config = replace(FusionConfig(), relevance={a: {T.TIMEOUT: 1.0, T.AUTH_FAILURE: 1.0} for a in order})
        types = {"jenkins_agent": T.TIMEOUT, "code_agent": T.AUTH_FAILURE}
        report = run(*(make(a, types[a], 0.7) for a in order), config=config)
        assert [s.failure_type for s in report.ranking] == [T.AUTH_FAILURE, T.TIMEOUT]
        # A tie between types that are not known compatible is a conflict, not a coin flip.
        assert report.conflict_status == ConflictStatus.CONFLICTED

    def test_more_supporting_agents_wins_a_score_tie(self):
        # Timeout: two agents at 0.5 -> noisy-OR 0.75. Auth: one agent at 0.75 -> 0.75.
        everywhere = {T.TIMEOUT: 1.0, T.AUTH_FAILURE: 1.0}
        config = replace(FusionConfig(), relevance={a: everywhere for a in ("jenkins_agent", "metrics_agent", "code_agent")})
        report = run(
            make("jenkins_agent", T.TIMEOUT, 0.5), make("metrics_agent", T.TIMEOUT, 0.5),
            make("code_agent", T.AUTH_FAILURE, 0.75), config=config,
        )
        assert [s.score for s in report.ranking] == [0.75, 0.75]
        assert report.ranking[0].failure_type == T.TIMEOUT  # name order alone would put auth_failure first

class TestCompleteness:
    def test_missing_agent_makes_report_incomplete_but_keeps_a_verdict(self):
        report = run(make("jenkins_agent", T.TIMEOUT, 0.9), make("metrics_agent", T.TIMEOUT, 0.8))
        assert report.completeness == Completeness.INCOMPLETE
        assert report.missing_agents == ["code_agent"]
        assert report.verdict == T.TIMEOUT
        assert not report.ready_for_policy_review
        assert report.blocking_reasons[0].code == BlockingCode.INCOMPLETE

    def test_failed_agent_casts_no_vote_and_report_is_incomplete(self):
        report = run(
            make("jenkins_agent", T.TIMEOUT, 0.9), make("metrics_agent", T.TIMEOUT, 0.8),
            make("code_agent", T.CONFIG_ERROR, 0.9, status="failed"),
        )
        assert report.failed_agents == ["code_agent"]
        assert [s.failure_type for s in report.ranking] == [T.TIMEOUT]
        assert report.completeness == Completeness.INCOMPLETE

    def test_complete_unconflicted_report_is_ready_for_policy_review(self):
        report = run(
            make("jenkins_agent", T.TIMEOUT, 0.9), make("metrics_agent", T.TIMEOUT, 0.8), make("code_agent", T.TIMEOUT, 0.7)
        )
        assert report.ready_for_policy_review and report.blocking_reasons == []


class TestConflict:
    ARGS = (make("jenkins_agent", T.AUTH_FAILURE, 0.7), make("code_agent", T.CONFIG_ERROR, 0.85))

    def test_far_apart_types_are_not_conflicted(self):
        report = run(make("jenkins_agent", T.DEPENDENCY_REGRESSION, 0.9), make("code_agent", T.CONFIG_ERROR, 0.4))
        assert report.conflict_status == ConflictStatus.NONE

    def test_close_and_unclassified_is_conflicted_and_tagged(self):
        report = run(*self.ARGS)
        assert report.conflict_status == ConflictStatus.CONFLICTED
        assert report.unclassified_pairs  # visibly tagged
        assert {T.AUTH_FAILURE, T.CONFIG_ERROR} <= set(report.conflicting_types)

    def test_close_and_known_compatible_is_not_conflicted(self):
        config = replace(FusionConfig(), trust_proposed_relations=True)
        report = run(make("jenkins_agent", T.TIMEOUT, 0.8), make("metrics_agent", T.RESOURCE_EXHAUSTION, 0.8), config=config)
        assert report.conflict_status == ConflictStatus.NONE

    def test_proposed_relation_is_ignored_until_signed_off(self):
        report = run(make("jenkins_agent", T.TIMEOUT, 0.8), make("metrics_agent", T.RESOURCE_EXHAUSTION, 0.8))
        assert report.conflict_status == ConflictStatus.CONFLICTED
        assert report.unclassified_pairs

    def test_close_and_signed_off_incompatible_is_conflicted_not_unclassified(self):
        pair = frozenset({T.AUTH_FAILURE, T.CONFIG_ERROR})
        config = replace(FusionConfig(), relations={pair: TypeRelation(RelationStatus.INCOMPATIBLE, True, "test")})
        report = run(*self.ARGS, config=config)
        assert report.conflict_status == ConflictStatus.CONFLICTED
        assert report.unclassified_pairs == []

    def test_margin_is_configurable(self):
        wide = replace(FusionConfig(), conflict_margin=0.7)
        report = run(make("jenkins_agent", T.DEPENDENCY_REGRESSION, 0.9), make("code_agent", T.CONFIG_ERROR, 0.4), config=wide)
        assert report.conflict_status == ConflictStatus.CONFLICTED
        assert BlockingCode.CONFLICTED in {b.code for b in report.blocking_reasons}


class TestNegativeEvidence:
    """Each row of the decision table in the Week 4 plan."""

    LEAD = make("jenkins_agent", T.TIMEOUT, 0.8)

    def neg(self, report):
        return score_of(report, T.TIMEOUT).negative_contributions

    def test_relevant_completed_adequate_coverage_gets_bounded_discount(self):
        report = run(self.LEAD, make("metrics_agent", T.UNKNOWN, 0.2, status="insufficient_evidence", coverage=0.9))
        [n] = self.neg(report)
        assert n.kind == NegativeKind.GENERAL_ABSENCE and n.status == NegativeStatus.APPLIED
        assert score_of(report, T.TIMEOUT).discount == pytest.approx(0.15 * 0.8)
        assert score_of(report, T.TIMEOUT).score < score_of(report, T.TIMEOUT).undiscounted_score

    @pytest.mark.parametrize("coverage", [0.0, None, 0.59])
    def test_no_data_unknown_or_low_coverage_is_neutral(self, coverage):
        report = run(self.LEAD, make("metrics_agent", T.UNKNOWN, 0.2, coverage=coverage))
        assert self.neg(report) == []
        assert score_of(report, T.TIMEOUT).discount == 0

    def test_failed_agent_has_no_vote_either_way(self):
        report = run(self.LEAD, make("metrics_agent", T.UNKNOWN, 0.2, status="failed", coverage=1.0))
        assert self.neg(report) == []

    def test_agent_not_relevant_to_the_type_does_not_vote_against_it(self):
        report = run(self.LEAD, make("code_agent", T.UNKNOWN, 0.2, coverage=1.0))  # code relevance to timeout 0.2
        assert self.neg(report) == []

    def test_explicit_contradiction_is_penalised_without_needing_coverage(self):
        report = run(self.LEAD, make("metrics_agent", T.UNKNOWN, 0.2, contradicts={T.TIMEOUT: ["metric-latency"]}))
        [n] = self.neg(report)
        assert n.kind == NegativeKind.EXPLICIT_CONTRADICTION and n.source_evidence_id == "metric-latency"

    def test_same_observation_as_contradiction_and_absence_is_penalised_once(self):
        report = run(
            self.LEAD,
            make("metrics_agent", T.UNKNOWN, 0.2, coverage=1.0, contradicts={T.TIMEOUT: ["metric-latency"]}),
        )
        by_kind = {n.kind: n for n in self.neg(report)}
        assert by_kind[NegativeKind.EXPLICIT_CONTRADICTION].status == NegativeStatus.APPLIED
        assert by_kind[NegativeKind.GENERAL_ABSENCE].status == NegativeStatus.SUPPRESSED_BY_EXPLICIT
        assert score_of(report, T.TIMEOUT).discount == pytest.approx(0.15 * 0.8)

    def test_distinct_evidence_ids_are_separate_penalties_up_to_the_cap(self):
        ids = [f"m{i}" for i in range(10)]
        report = run(self.LEAD, make("metrics_agent", T.UNKNOWN, 0.2, contradicts={T.TIMEOUT: ids}))
        top = score_of(report, T.TIMEOUT)
        assert top.discount == pytest.approx(0.30)  # capped, never more
        assert any(n.status == NegativeStatus.CAPPED for n in top.negative_contributions)
        assert sum(n.applied_penalty for n in top.negative_contributions) == pytest.approx(0.30)

    def test_an_agents_own_primary_type_is_not_discounted_by_its_own_doubts(self):
        ev = make("jenkins_agent", T.TIMEOUT, 0.8, contradicts={T.TIMEOUT: ["x"]})
        assert score_of(run(ev), T.TIMEOUT).negative_contributions == []

    def test_negative_evidence_can_flip_close_ranking(self):
        a = make("jenkins_agent", T.TIMEOUT, 0.8)
        b = make("code_agent", T.CONFIG_ERROR, 0.72)
        before = run(a, b)
        after = run(a, b, make("metrics_agent", T.UNKNOWN, 0.2, coverage=1.0))
        assert score_of(after, T.TIMEOUT).score < score_of(before, T.TIMEOUT).score


def test_next_steps_are_deduplicated_and_verdict_agents_come_first():
    report = run(
        make("code_agent", T.CONFIG_ERROR, 0.4, steps=["restore config", "shared step"]),
        make("jenkins_agent", T.DEPENDENCY_REGRESSION, 0.9, steps=["pin dependency", "shared step"]),
    )
    assert report.recommended_next_steps == ["pin dependency", "shared step", "restore config"]


def test_agreement_matrix_covers_every_pair():
    report = run(
        make("jenkins_agent", T.TIMEOUT, 0.9), make("metrics_agent", T.TIMEOUT, 0.8), make("code_agent", T.UNKNOWN, 0.1)
    )
    relations = {p.agents: p.relation.value for p in report.agreement}
    assert relations == {
        ("code_agent", "jenkins_agent"): "no_signal",
        ("code_agent", "metrics_agent"): "no_signal",
        ("jenkins_agent", "metrics_agent"): "agree",
    }

