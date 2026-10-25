"""The fused report: the Coordinator's derived conclusions plus their provenance (P2).

**Owner:** P2 (fusion slice), built on P3's Coordinator skeleton. The report is *derived*
from the agents' evidence and never replaces it: each agent's own verdict is copied in
unchanged under ``agent_verdicts`` and the original documents stay on the blackboard.

The fused ``confidence`` and every ``score`` here are **uncalibrated scores in [0, 1]**,
not probabilities. They order hypotheses and drive thresholds; they have not been
validated against ground truth. Verdict, completeness and conflict are separate fields on
purpose: an incomplete report can still show a defensible leading hypothesis, and a
complete one can still be conflicted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.models import FailureTaxonomy


class FusionMethod(str, Enum):
    NOISY_OR = "noisy_or"
    WEIGHTED_MEAN = "weighted_mean"


class Completeness(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class ConflictStatus(str, Enum):
    NONE = "none"
    CONFLICTED = "conflicted"


class RelationStatus(str, Enum):
    """Whether two failure types may both be true at once (for conflict detection only)."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNCLASSIFIED = "unclassified"


class NegativeKind(str, Enum):
    EXPLICIT_CONTRADICTION = "explicit_contradiction"
    GENERAL_ABSENCE = "general_absence"


class NegativeStatus(str, Enum):
    APPLIED = "applied"
    SUPPRESSED_BY_EXPLICIT = "suppressed_by_explicit"
    CAPPED = "capped"


class AgentRelation(str, Enum):
    AGREE = "agree"
    COMPATIBLE = "compatible"
    DISAGREE = "disagree"
    NO_SIGNAL = "no_signal"


class BlockingCode(str, Enum):
    INCOMPLETE = "incomplete"
    CONFLICTED = "conflicted"
    UNKNOWN_VERDICT = "unknown_verdict"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contribution(_Model):
    """One agent's vote for one failure type: raw confidence, relevance and their product."""

    agent: str
    failure_type: FailureTaxonomy
    confidence: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    weighted: float = Field(ge=0, le=1)


class NegativeContribution(_Model):
    """One piece of negative evidence against a failure type, with its dedup key."""

    agent: str
    failure_type: FailureTaxonomy
    kind: NegativeKind
    source_evidence_id: str
    penalty: float = Field(ge=0)
    status: NegativeStatus
    applied_penalty: float = Field(ge=0)


class TypeScore(_Model):
    failure_type: FailureTaxonomy
    score: float = Field(ge=0, le=1, description="Selected method, after any negative discount.")
    alternative_score: float = Field(ge=0, le=1, description="The other method, same discount.")
    undiscounted_score: float = Field(ge=0, le=1)
    discount: float = Field(ge=0, le=1)
    supporting_agents: list[str]
    contributions: list[Contribution]
    negative_contributions: list[NegativeContribution] = Field(default_factory=list)


class AgentVerdict(_Model):
    """What one agent concluded, copied unchanged from its evidence document."""

    agent: str
    status: str
    failure_type: FailureTaxonomy
    confidence: float = Field(ge=0, le=1)
    summary: str
    investigation_coverage: float | None = None
    coverage_notes: list[str] = Field(default_factory=list)
    voted: bool = Field(description="Whether this verdict entered the fused score.")
    vote_note: str = ""
    alternative_hypotheses: list[str] = Field(
        default_factory=list,
        description="Other hypotheses the agent listed. Shown, not scored.",
    )


class AgentPairAgreement(_Model):
    agents: tuple[str, str]
    relation: AgentRelation
    relation_status: RelationStatus | None = None


class BlockingReason(_Model):
    code: BlockingCode
    detail: str


class FusedReport(_Model):
    incident_id: UUID
    generated_at: datetime
    schema_version: str = "1"

    verdict: FailureTaxonomy
    confidence: float = Field(ge=0, le=1, description="Fused score; uncalibrated, not a probability.")
    verdict_reason: str

    completeness: Completeness
    missing_agents: list[str] = Field(default_factory=list)
    failed_agents: list[str] = Field(default_factory=list)

    conflict_status: ConflictStatus
    conflicting_types: list[FailureTaxonomy] = Field(default_factory=list)
    unclassified_pairs: list[tuple[FailureTaxonomy, FailureTaxonomy]] = Field(default_factory=list)

    fusion_method: FusionMethod
    ranking: list[TypeScore] = Field(default_factory=list)
    agent_verdicts: list[AgentVerdict] = Field(default_factory=list)
    agreement: list[AgentPairAgreement] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)

    ready_for_policy_review: bool
    blocking_reasons: list[BlockingReason] = Field(default_factory=list)

    narrative: str
    rules_fired: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _pct(value: float) -> str:
    return f"{value:.2f}"


def render_markdown(report: FusedReport) -> str:
    """Render the report for people. Every number here is also in the JSON form."""
    lines: list[str] = [f"# Incident report {report.incident_id}", ""]
    lines += [
        (
            f"- **Verdict:** `{report.verdict.value}` (fused score {_pct(report.confidence)}, "
            f"uncalibrated; method `{report.fusion_method.value}`)"
        ),
        f"- **Completeness:** {report.completeness.value}"
        + (f" (missing: {', '.join(report.missing_agents)})" if report.missing_agents else "")
        + (f" (failed: {', '.join(report.failed_agents)})" if report.failed_agents else ""),
        f"- **Conflict:** {report.conflict_status.value}"
        + (f" ({', '.join(t.value for t in report.conflicting_types)})" if report.conflicting_types else ""),
        (
            f"- **Ready for policy review:** {'yes' if report.ready_for_policy_review else 'no'}"
            " (precondition only; the policy layer decides)"
        ),
        f"- **Generated:** {report.generated_at.isoformat()}",
        "",
        "## Summary",
        "",
        report.narrative,
        "",
    ]
    if report.blocking_reasons:
        lines += ["## Blocking reasons", ""]
        lines += [f"- `{b.code.value}`: {b.detail}" for b in report.blocking_reasons]
        lines.append("")

    lines += ["## What each agent concluded", "", "| Agent | Status | Verdict | Confidence | Coverage | In score |", "|---|---|---|---|---|---|"]
    for v in report.agent_verdicts:
        coverage = "n/a" if v.investigation_coverage is None else _pct(v.investigation_coverage)
        note = "yes" if v.voted else f"no ({v.vote_note})" if v.vote_note else "no"
        lines.append(f"| {v.agent} | {v.status} | `{v.failure_type.value}` | {_pct(v.confidence)} | {coverage} | {note} |")
    lines.append("")

    if report.ranking:
        lines += [
            "## Ranking",
            "",
            "| # | Failure type | Score | Other method | Discount | Supported by |",
            "|---|---|---|---|---|---|",
        ]
        for rank, item in enumerate(report.ranking, start=1):
            lines.append(
                f"| {rank} | `{item.failure_type.value}` | {_pct(item.score)} | {_pct(item.alternative_score)} "
                f"| {_pct(item.discount)} | {', '.join(item.supporting_agents)} |"
            )
        lines.append("")
        lines += ["### Contributions (raw confidence x relevance)", ""]
        for item in report.ranking:
            for c in item.contributions:
                lines.append(
                    f"- `{item.failure_type.value}` <- {c.agent}: {_pct(c.confidence)} x {_pct(c.relevance)} = {_pct(c.weighted)}"
                )
            for n in item.negative_contributions:
                lines.append(
                    f"- `{item.failure_type.value}` against, {n.agent} ({n.kind.value}, `{n.source_evidence_id}`): "
                    f"-{_pct(n.applied_penalty)} [{n.status.value}]"
                )
        lines.append("")

    if report.agreement:
        lines += ["## Agreement between agents", ""]
        for pair in report.agreement:
            status = f" [{pair.relation_status.value}]" if pair.relation_status else ""
            lines.append(f"- {pair.agents[0]} / {pair.agents[1]}: {pair.relation.value}{status}")
        lines.append("")

    if report.unclassified_pairs:
        lines += ["## Unclassified type pairs (treated as conflicting until signed off)", ""]
        lines += [f"- `{a.value}` / `{b.value}`" for a, b in report.unclassified_pairs]
        lines.append("")

    if report.recommended_next_steps:
        lines += ["## Recommended next steps", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(report.recommended_next_steps, start=1)]
        lines.append("")

    lines += ["## Provenance", ""]
    lines += [f"- {rule}" for rule in report.rules_fired]
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "AgentPairAgreement",
    "AgentRelation",
    "AgentVerdict",
    "BlockingCode",
    "BlockingReason",
    "Completeness",
    "ConflictStatus",
    "Contribution",
    "FusedReport",
    "FusionMethod",
    "NegativeContribution",
    "NegativeKind",
    "NegativeStatus",
    "RelationStatus",
    "TypeScore",
    "render_markdown",
]
