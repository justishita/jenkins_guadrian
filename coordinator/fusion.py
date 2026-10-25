"""Deterministic fusion of the three agents' evidence into an ``FusedReport``.

**Owner:** P2 (fusion slice), on top of P3's ``BlackboardView``. ``fuse`` is a pure
function: for a fixed view, configuration and ``now`` it returns the same report
regardless of the order the agents' documents arrive in.

What is scored. Each agent contributes **one vote**: its primary verdict
(``failure_type``, ``confidence``). Other hypotheses an agent lists are surfaced in the
report but not scored, because their confidences are not comparable across agents (a
Jenkins agent can list a secondary hypothesis above its own primary). A vote is
``confidence x relevance`` where relevance is how much that agent's evidence domain
speaks to that failure type. Both tables below are *proposals* that need team sign-off.

The fused number is an uncalibrated score, never a probability.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import prod

from common.models import Evidence, FailureTaxonomy
from coordinator.agent import BlackboardView
from coordinator.report import (
    AgentPairAgreement,
    AgentRelation,
    AgentVerdict,
    BlockingCode,
    BlockingReason,
    Completeness,
    ConflictStatus,
    Contribution,
    FusionMethod,
    FusedReport,
    NegativeContribution,
    NegativeKind,
    NegativeStatus,
    RelationStatus,
    TypeScore,
)

T = FailureTaxonomy

#: How much each agent's evidence domain speaks to each failure type (0 = not at all).
#: PROPOSED values, pending team sign-off. Rationale: Jenkins reads build logs, so it is
#: strongest on build-time causes; Metrics sees runtime resources and latency; Code sees
#: what changed in the commit.
AGENT_RELEVANCE: dict[str, dict[FailureTaxonomy, float]] = {
    "jenkins_agent": {
        T.CODE_TEST_FAILURE: 1.0, T.BUILD_COMPILATION_FAILURE: 1.0, T.DEPENDENCY_REGRESSION: 1.0,
        T.TIMEOUT: 0.9, T.RESOURCE_EXHAUSTION: 0.6, T.INFRA_NETWORK_FAILURE: 0.7,
        T.CONFIG_ERROR: 0.9, T.AUTH_FAILURE: 1.0, T.FLAKY_TEST: 0.8, T.DEPLOYMENT_FAILURE: 1.0,
    },
    "metrics_agent": {
        T.CODE_TEST_FAILURE: 0.3, T.BUILD_COMPILATION_FAILURE: 0.2, T.DEPENDENCY_REGRESSION: 0.2,
        T.TIMEOUT: 0.8, T.RESOURCE_EXHAUSTION: 1.0, T.INFRA_NETWORK_FAILURE: 0.9,
        T.CONFIG_ERROR: 0.3, T.AUTH_FAILURE: 0.1, T.FLAKY_TEST: 0.2, T.DEPLOYMENT_FAILURE: 0.5,
    },
    "code_agent": {
        T.CODE_TEST_FAILURE: 1.0, T.BUILD_COMPILATION_FAILURE: 1.0, T.DEPENDENCY_REGRESSION: 0.8,
        T.TIMEOUT: 0.2, T.RESOURCE_EXHAUSTION: 0.2, T.INFRA_NETWORK_FAILURE: 0.1,
        T.CONFIG_ERROR: 0.8, T.AUTH_FAILURE: 0.3, T.FLAKY_TEST: 0.5, T.DEPLOYMENT_FAILURE: 0.4,
    },
}


@dataclass(frozen=True)
class TypeRelation:
    status: RelationStatus
    signed_off: bool
    rationale: str


def _pair(a: FailureTaxonomy, b: FailureTaxonomy) -> frozenset[FailureTaxonomy]:
    return frozenset((a, b))


#: Whether two failure types can both be true at once. For conflict detection only: it does
#: not claim the types are equivalent or that one causes the other. Every entry is
#: ``proposed`` until the team signs it off pair by pair; anything absent is unclassified
#: and treated conservatively as conflicting.
TYPE_RELATIONS: dict[frozenset[FailureTaxonomy], TypeRelation] = {
    _pair(T.RESOURCE_EXHAUSTION, T.TIMEOUT): TypeRelation(
        RelationStatus.COMPATIBLE, False, "resource pressure commonly shows up as timeouts"),
    _pair(T.INFRA_NETWORK_FAILURE, T.TIMEOUT): TypeRelation(
        RelationStatus.COMPATIBLE, False, "network failures commonly show up as timeouts"),
    _pair(T.DEPENDENCY_REGRESSION, T.BUILD_COMPILATION_FAILURE): TypeRelation(
        RelationStatus.COMPATIBLE, False, "a bad dependency can surface as a compile failure"),
    _pair(T.CONFIG_ERROR, T.DEPLOYMENT_FAILURE): TypeRelation(
        RelationStatus.COMPATIBLE, False, "a config error can be why a deployment failed"),
    _pair(T.CODE_TEST_FAILURE, T.FLAKY_TEST): TypeRelation(
        RelationStatus.COMPATIBLE, False, "a failing test can also be intermittently flaky"),
}


@dataclass(frozen=True)
class FusionConfig:
    method: FusionMethod = FusionMethod.NOISY_OR
    #: An agent may vote against a type only if its relevance to that type is at least this.
    min_relevance_for_negative: float = 0.5
    #: "Found nothing" counts as evidence of absence only at or above this declared coverage.
    min_coverage: float = 0.6
    #: Penalty per negative contribution = negative_strength x the agent's relevance.
    negative_strength: float = 0.15
    #: Total negative discount on one type can never exceed this.
    discount_cap: float = 0.30
    #: Types whose score is within this of the leader, and not known-compatible, conflict.
    conflict_margin: float = 0.10
    #: Honour ``proposed`` (not yet signed-off) relations. Off: they are treated as unclassified.
    trust_proposed_relations: bool = False
    relevance: Mapping[str, Mapping[FailureTaxonomy, float]] = field(default_factory=lambda: AGENT_RELEVANCE)
    relations: Mapping[frozenset[FailureTaxonomy], TypeRelation] = field(default_factory=lambda: TYPE_RELATIONS)

    def snapshot(self) -> dict[str, object]:
        return {
            "method": self.method.value,
            "min_relevance_for_negative": self.min_relevance_for_negative,
            "min_coverage": self.min_coverage,
            "negative_strength": self.negative_strength,
            "discount_cap": self.discount_cap,
            "conflict_margin": self.conflict_margin,
            "trust_proposed_relations": self.trust_proposed_relations,
            "uncalibrated": True,
        }

    def relevance_of(self, agent: str, failure_type: FailureTaxonomy) -> float:
        return self.relevance.get(agent, {}).get(failure_type, 0.0)

    def relation_of(self, a: FailureTaxonomy, b: FailureTaxonomy) -> RelationStatus:
        if a == b:
            return RelationStatus.COMPATIBLE
        entry = self.relations.get(_pair(a, b))
        if entry is None or (not entry.signed_off and not self.trust_proposed_relations):
            return RelationStatus.UNCLASSIFIED
        return entry.status


def noisy_or(weighted: list[float]) -> float:
    return 1.0 - prod(1.0 - w for w in weighted)


def weighted_mean(weighted: list[float], weights: list[float]) -> float:
    total = sum(weights)
    return sum(weighted) / total if total else 0.0


def _vote_note(evidence: Evidence, config: FusionConfig) -> str:
    """Empty if the agent's primary verdict counts as a vote, else why not."""
    if evidence.status == "failed":
        return "agent failed"
    if evidence.failure_type == FailureTaxonomy.UNKNOWN:
        return "no failure type identified"
    if evidence.confidence <= 0:
        return "zero confidence"
    if config.relevance_of(evidence.agent, evidence.failure_type) <= 0:
        return "outside the agent's domain"
    return ""


def _negative_contributions(
    evidence_by_agent: dict[str, Evidence], scored: set[FailureTaxonomy], config: FusionConfig
) -> dict[FailureTaxonomy, list[NegativeContribution]]:
    """Gated, deduplicated, capped negative evidence per scored failure type."""
    raw: dict[tuple[str, FailureTaxonomy], dict[str, tuple[NegativeKind, float]]] = {}

    for agent, evidence in sorted(evidence_by_agent.items()):
        if evidence.status == "failed":
            continue  # a failed agent casts no vote, for or against
        for ftype in sorted(scored, key=lambda t: t.value):
            if ftype == evidence.failure_type:
                continue  # the agent's own doubt is already inside its confidence
            relevance = config.relevance_of(agent, ftype)
            if relevance < config.min_relevance_for_negative:
                continue
            penalty = config.negative_strength * relevance
            slot = raw.setdefault((agent, ftype), {})
            for hypothesis in evidence.root_cause_hypotheses:
                if hypothesis.failure_type == ftype:
                    for cited in hypothesis.contradicting_evidence:
                        slot.setdefault(cited, (NegativeKind.EXPLICIT_CONTRADICTION, penalty))
            if (
                evidence.failure_type == FailureTaxonomy.UNKNOWN
                and evidence.investigation_coverage is not None
                and evidence.investigation_coverage >= config.min_coverage
            ):
                slot.setdefault(f"{agent}:no_signal", (NegativeKind.GENERAL_ABSENCE, penalty))

    result: dict[FailureTaxonomy, list[NegativeContribution]] = {}
    remaining: dict[FailureTaxonomy, float] = {t: config.discount_cap for t in scored}
    for (agent, ftype), slot in sorted(raw.items(), key=lambda kv: (kv[0][1].value, kv[0][0])):
        has_explicit = any(kind == NegativeKind.EXPLICIT_CONTRADICTION for kind, _ in slot.values())
        for source_id, (kind, penalty) in sorted(slot.items()):
            if kind == NegativeKind.GENERAL_ABSENCE and has_explicit:
                status, applied = NegativeStatus.SUPPRESSED_BY_EXPLICIT, 0.0
            else:
                applied = min(penalty, remaining[ftype])
                remaining[ftype] -= applied
                status = NegativeStatus.APPLIED if applied >= penalty - 1e-12 else NegativeStatus.CAPPED
            result.setdefault(ftype, []).append(
                NegativeContribution(
                    agent=agent, failure_type=ftype, kind=kind, source_evidence_id=source_id,
                    penalty=round(penalty, 6), status=status, applied_penalty=round(applied, 6),
                )
            )
    return result


def _agreement(
    voters: dict[str, Evidence], evidence_by_agent: dict[str, Evidence], config: FusionConfig
) -> list[AgentPairAgreement]:
    names = sorted(evidence_by_agent)
    pairs: list[AgentPairAgreement] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a not in voters or b not in voters:
                pairs.append(AgentPairAgreement(agents=(a, b), relation=AgentRelation.NO_SIGNAL))
                continue
            ta, tb = voters[a].failure_type, voters[b].failure_type
            if ta == tb:
                pairs.append(AgentPairAgreement(agents=(a, b), relation=AgentRelation.AGREE))
                continue
            status = config.relation_of(ta, tb)
            relation = AgentRelation.COMPATIBLE if status == RelationStatus.COMPATIBLE else AgentRelation.DISAGREE
            pairs.append(AgentPairAgreement(agents=(a, b), relation=relation, relation_status=status))
    return pairs


def _narrative(
    verdict: FailureTaxonomy, confidence: float, reason: str, completeness: Completeness,
    missing: list[str], failed: list[str], conflict: ConflictStatus,
    conflicting: list[FailureTaxonomy], ranking: list[TypeScore],
) -> str:
    if verdict == FailureTaxonomy.UNKNOWN:
        text = f"No failure type could be established. {reason}"
    else:
        support = ", ".join(ranking[0].supporting_agents)
        text = f"The leading hypothesis is {verdict.value} (fused score {confidence:.2f}, uncalibrated), supported by {support}."
        if len(ranking) > 1:
            runner = ranking[1]
            text += f" The next candidate is {runner.failure_type.value} ({runner.score:.2f})."
    if completeness == Completeness.INCOMPLETE:
        parts = []
        if missing:
            parts.append(f"no evidence from {', '.join(missing)}")
        if failed:
            parts.append(f"{', '.join(failed)} failed")
        text += f" The investigation is incomplete: {'; '.join(parts)}."
    if conflict == ConflictStatus.CONFLICTED:
        text += f" The result is conflicted between {', '.join(t.value for t in conflicting)}."
    return text


def fuse(view: BlackboardView, config: FusionConfig | None = None, *, now: datetime) -> FusedReport:
    """Fuse one incident's evidence. Pure and deterministic; ``now`` is the only clock."""
    config = config or FusionConfig()
    evidence_by_agent = {e.agent: e for e in sorted(view.evidence, key=lambda e: (e.agent, e.created_at))}
    rules: list[str] = []

    missing = sorted(set(view.missing_agents))
    failed = sorted(a for a, e in evidence_by_agent.items() if e.status == "failed")

    voters: dict[str, Evidence] = {}
    verdicts: list[AgentVerdict] = []
    for agent, ev in evidence_by_agent.items():
        note = _vote_note(ev, config)
        if not note:
            voters[agent] = ev
        verdicts.append(
            AgentVerdict(
                agent=agent, status=ev.status, failure_type=ev.failure_type, confidence=ev.confidence,
                summary=ev.summary, investigation_coverage=ev.investigation_coverage,
                coverage_notes=list(ev.coverage_notes), voted=not note, vote_note=note,
                alternative_hypotheses=sorted(
                    {h.failure_type.value for h in ev.root_cause_hypotheses if h.failure_type != ev.failure_type}
                ),
            )
        )
    rules.append(f"one vote per agent: primary verdict only ({len(voters)} of {len(evidence_by_agent)} eligible)")

    by_type: dict[FailureTaxonomy, list[Contribution]] = {}
    for agent, ev in voters.items():
        relevance = config.relevance_of(agent, ev.failure_type)
        by_type.setdefault(ev.failure_type, []).append(
            Contribution(
                agent=agent, failure_type=ev.failure_type, confidence=ev.confidence,
                relevance=relevance, weighted=round(ev.confidence * relevance, 6),
            )
        )

    negatives = _negative_contributions(evidence_by_agent, set(by_type), config)
    if any(negatives.values()):
        rules.append("negative evidence applied with dedup and cap")

    scores: list[TypeScore] = []
    for ftype, contributions in by_type.items():
        contributions.sort(key=lambda c: c.agent)
        weighted = [c.weighted for c in contributions]
        weights = [c.relevance for c in contributions]
        primary = noisy_or(weighted) if config.method == FusionMethod.NOISY_OR else weighted_mean(weighted, weights)
        other = weighted_mean(weighted, weights) if config.method == FusionMethod.NOISY_OR else noisy_or(weighted)
        negs = negatives.get(ftype, [])
        discount = min(config.discount_cap, sum(n.applied_penalty for n in negs))
        scores.append(
            TypeScore(
                failure_type=ftype,
                score=round(primary * (1 - discount), 6),
                alternative_score=round(other * (1 - discount), 6),
                undiscounted_score=round(primary, 6),
                discount=round(discount, 6),
                supporting_agents=[c.agent for c in contributions],
                contributions=contributions,
                negative_contributions=negs,
            )
        )
    scores.sort(
        key=lambda s: (-s.score, -len(s.supporting_agents), -max(c.weighted for c in s.contributions), s.failure_type.value)
    )
    if len(scores) > 1:
        rules.append("ties broken by supporting agents, then strongest single contribution, then type name")

    conflicting: list[FailureTaxonomy] = []
    unclassified: list[tuple[FailureTaxonomy, FailureTaxonomy]] = []
    if scores:
        verdict, confidence = scores[0].failure_type, scores[0].score
        reason = f"{verdict.value} has the highest fused score."
        for other in scores[1:]:
            if scores[0].score - other.score >= config.conflict_margin:
                continue
            status = config.relation_of(verdict, other.failure_type)
            if status == RelationStatus.COMPATIBLE:
                continue
            conflicting.append(other.failure_type)
            if status == RelationStatus.UNCLASSIFIED:
                unclassified.append(tuple(sorted((verdict, other.failure_type), key=lambda t: t.value)))  # type: ignore[arg-type]
        if conflicting:
            conflicting = [verdict, *conflicting]
            rules.append(f"conflict: runner-up within {config.conflict_margin} of the leader and not known-compatible")
    else:
        verdict, confidence = FailureTaxonomy.UNKNOWN, 0.0
        reason = "No agent produced an eligible vote for any failure type."
        rules.append("no eligible vote: verdict is unknown, no winner taken from zero scores")

    completeness = Completeness.INCOMPLETE if (missing or failed) else Completeness.COMPLETE
    conflict = ConflictStatus.CONFLICTED if conflicting else ConflictStatus.NONE

    blocking: list[BlockingReason] = []
    if completeness == Completeness.INCOMPLETE:
        names = ", ".join([*missing, *(f"{a} (failed)" for a in failed)])
        blocking.append(BlockingReason(code=BlockingCode.INCOMPLETE, detail=f"no usable evidence from: {names}"))
    if conflict == ConflictStatus.CONFLICTED:
        blocking.append(
            BlockingReason(code=BlockingCode.CONFLICTED, detail="unresolved between: " + ", ".join(t.value for t in conflicting))
        )
    if verdict == FailureTaxonomy.UNKNOWN:
        blocking.append(BlockingReason(code=BlockingCode.UNKNOWN_VERDICT, detail=reason))

    steps: list[str] = []
    ordered = sorted(
        evidence_by_agent.values(), key=lambda e: (e.failure_type != verdict, e.agent)
    )
    for ev in ordered:
        for step in ev.recommended_next_steps:
            if step not in steps:
                steps.append(step)

    return FusedReport(
        incident_id=view.incident_id,
        generated_at=now,
        verdict=verdict,
        confidence=confidence,
        verdict_reason=reason,
        completeness=completeness,
        missing_agents=missing,
        failed_agents=failed,
        conflict_status=conflict,
        conflicting_types=conflicting,
        unclassified_pairs=unclassified,
        fusion_method=config.method,
        ranking=scores,
        agent_verdicts=verdicts,
        agreement=_agreement(voters, evidence_by_agent, config),
        recommended_next_steps=steps,
        ready_for_policy_review=not blocking,
        blocking_reasons=blocking,
        narrative=_narrative(verdict, confidence, reason, completeness, missing, failed, conflict, conflicting, scores),
        rules_fired=rules,
        config=config.snapshot(),
    )


__all__ = [
    "AGENT_RELEVANCE",
    "TYPE_RELATIONS",
    "FusionConfig",
    "TypeRelation",
    "fuse",
    "noisy_or",
    "weighted_mean",
]
