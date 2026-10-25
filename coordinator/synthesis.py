"""Week 4: fuse three independent evidence documents into one incident report.

**Owner:** P3. This is the step the whole multi-agent design exists for. Three agents
investigate the same failure from different evidence and never speak to each other;
this is the only place their conclusions meet.

Fusion is deterministic and explainable, for the same reason the agents' own reasoning
is: the rules are the baseline that LLM-assisted synthesis gets measured against, they
run in tests with no API quota, and a fusion that goes wrong can be read and corrected.

Three principles decide everything here:

* **Agreement is evidence.** Two agents independently reaching the same failure type
  from different data is far stronger than either alone, and the confidence says so.
* **Disagreement is not noise.** When agents conflict, the report names the conflict
  and lowers confidence rather than quietly picking a winner.
* **Abstention is a finding.** An agent that correctly says "my evidence does not
  explain this" has contributed something real, and it is recorded as such.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.a2a_contract import AGENT_NAMES
from common.models import Evidence, EvidenceHypothesis, FailureTaxonomy


#: Below this the report names no cause. Same floor the agents and the policy gate use,
#: so a report can never recommend a fix the gate would refuse to act on.
MIN_REPORTABLE_CONFIDENCE = 0.5

#: A statement of ignorance may not be made confidently, at any level of the system.
MAX_UNKNOWN_CONFIDENCE = 0.4

#: What a second agent independently agreeing is worth. Deliberately modest: two
#: agents can be wrong together, and agreement raises confidence rather than settling it.
AGREEMENT_BONUS = 0.1

#: What an unresolved conflict costs. Larger than the agreement bonus on purpose -
#: being contradicted should hurt more than being corroborated helps.
CONFLICT_PENALTY = 0.15

#: Two causes backed by the same number of agents only tie when their strongest claims are
#: this close. A 0.90 claim against a 0.55 one is a clear lead, not a deadlock.
TIE_MARGIN = 0.10

ABSTAINING_STATUSES = frozenset({"insufficient_evidence", "failed"})


class AgentFinding(BaseModel):
    """One agent's contribution, as the report records it."""

    model_config = ConfigDict(frozen=True)

    agent: str
    status: str
    failure_type: FailureTaxonomy
    confidence: float = Field(ge=0, le=1)
    summary: str
    abstained: bool

    @property
    def is_substantive(self) -> bool:
        """True when the agent actually named a cause rather than declining to."""
        return not self.abstained and self.failure_type is not FailureTaxonomy.UNKNOWN


class Conflict(BaseModel):
    """Two agents naming different causes for the same incident."""

    model_config = ConfigDict(frozen=True)

    failure_type: FailureTaxonomy
    claimed_by: tuple[str, ...]
    confidence: float = Field(ge=0, le=1)

    def describe(self) -> str:
        return f"{', '.join(self.claimed_by)} say {self.failure_type.value} ({self.confidence:.2f})"


class IncidentReport(BaseModel):
    """The unified view of one incident, fused from every agent that reported.

    This is the structured output the rest of the system acts on: the remediation
    engine reads ``failure_type`` and ``confidence``, and the policy gate is handed a
    proposal built from it.
    """

    model_config = ConfigDict(frozen=True)

    incident_id: UUID
    created_at: datetime
    status: Literal["resolved_cause", "insufficient_evidence", "conflicting"]
    failure_type: FailureTaxonomy
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1)
    findings: tuple[AgentFinding, ...] = ()
    agreeing_agents: tuple[str, ...] = ()
    abstaining_agents: tuple[str, ...] = ()
    missing_agents: tuple[str, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    hypotheses: tuple[EvidenceHypothesis, ...] = ()
    recommended_next_steps: tuple[str, ...] = ()

    @field_validator("created_at")
    @classmethod
    def ensure_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @property
    def actionable(self) -> bool:
        """Whether a remediation may be proposed from this report at all."""
        return (
            self.status == "resolved_cause"
            and self.failure_type is not FailureTaxonomy.UNKNOWN
            and self.confidence >= MIN_REPORTABLE_CONFIDENCE
        )

    @property
    def reporting_agents(self) -> tuple[str, ...]:
        return tuple(finding.agent for finding in self.findings)

    @property
    def failed_agents(self) -> tuple[str, ...]:
        return tuple(finding.agent for finding in self.findings if finding.status == "failed")

    @property
    def complete(self) -> bool:
        """Every expected agent reported and none of them failed.

        Independent of ``actionable``: an incomplete report can still name a cause, but a
        reader must know that part of the investigation is missing.
        """
        return not self.missing_agents and not self.failed_agents

    def to_markdown(self) -> str:
        """Render the report for a human reviewer."""
        lines = [
            f"# Incident {self.incident_id}",
            "",
            f"**Cause:** {self.failure_type.value}  ",
            f"**Confidence:** {self.confidence:.2f}  ",
            f"**Status:** {self.status}" + ("" if self.complete else "  "),
            *(
                []
                if self.complete
                else [
                    "**Incomplete:** "
                    + "; ".join(
                        part
                        for part in (
                            f"no evidence from {', '.join(self.missing_agents)}" if self.missing_agents else "",
                            f"{', '.join(self.failed_agents)} failed" if self.failed_agents else "",
                        )
                        if part
                    )
                ]
            ),
            "",
            self.summary,
            "",
            "## What each agent found",
            "",
            "| Agent | Verdict | Confidence | Status |",
            "| --- | --- | --- | --- |",
        ]
        for finding in self.findings:
            lines.append(
                f"| {finding.agent} | {finding.failure_type.value} | "
                f"{finding.confidence:.2f} | {finding.status} |"
            )
        if self.conflicts:
            lines += ["", "## Unresolved disagreement", ""]
            lines += [f"- {conflict.describe()}" for conflict in self.conflicts]
        if self.recommended_next_steps:
            lines += ["", "## Recommended next steps", ""]
            lines += [f"1. {step}" for step in self.recommended_next_steps]
        return "\n".join(lines)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, round(value, 2)))


def _finding_from(evidence: Evidence) -> AgentFinding:
    abstained = (
        evidence.status in ABSTAINING_STATUSES
        or evidence.failure_type is FailureTaxonomy.UNKNOWN
    )
    return AgentFinding(
        agent=evidence.agent,
        status=evidence.status,
        failure_type=evidence.failure_type,
        confidence=evidence.confidence,
        summary=evidence.summary,
        abstained=abstained,
    )


def _namespaced(evidence: Evidence, hypothesis: EvidenceHypothesis) -> EvidenceHypothesis:
    """Prefix the cited evidence IDs with the agent that owns them.

    Each agent numbers its own evidence independently, so ``file-1`` means different
    things to the code and Jenkins agents. Without this the fused report would cite
    ambiguous IDs - the one way a merged report can quietly become unfalsifiable.
    """
    return hypothesis.model_copy(
        update={
            "supporting_evidence": [f"{evidence.agent}:{i}" for i in hypothesis.supporting_evidence],
            "contradicting_evidence": [
                f"{evidence.agent}:{i}" for i in hypothesis.contradicting_evidence
            ],
        }
    )


def _all_hypotheses(documents: Sequence[Evidence]) -> tuple[EvidenceHypothesis, ...]:
    ranked = [
        _namespaced(document, hypothesis)
        for document in documents
        for hypothesis in document.root_cause_hypotheses
    ]
    ranked.sort(key=lambda item: item.confidence, reverse=True)
    return tuple(ranked)


def _next_steps(documents: Sequence[Evidence], failure_type: FailureTaxonomy) -> tuple[str, ...]:
    """Take the steps from the agents that actually named this cause, in order."""
    steps: list[str] = []
    for document in documents:
        if document.failure_type is not failure_type:
            continue
        for step in document.recommended_next_steps:
            if step not in steps:
                steps.append(step)
    return tuple(steps)


def synthesize(
    evidence: Iterable[Evidence],
    *,
    now: datetime | None = None,
    expected_agents: Sequence[str] = AGENT_NAMES,
) -> IncidentReport:
    """Fuse every agent's evidence for one incident into a single report.

    Raises ``ValueError`` when handed nothing, or documents for different incidents -
    a report that silently mixed two incidents would be worse than no report.
    """
    documents = sorted(evidence, key=lambda item: item.agent)
    if not documents:
        raise ValueError("cannot synthesize a report from no evidence")

    incident_ids = {document.incident_id for document in documents}
    if len(incident_ids) > 1:
        raise ValueError(f"evidence spans {len(incident_ids)} incidents: {sorted(map(str, incident_ids))}")

    incident_id = incident_ids.pop()
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    findings = tuple(_finding_from(document) for document in documents)
    abstaining = tuple(finding.agent for finding in findings if finding.abstained)
    substantive = [finding for finding in findings if finding.is_substantive]
    hypotheses = _all_hypotheses(documents)
    reporting = {finding.agent for finding in findings}
    missing = tuple(agent for agent in expected_agents if agent not in reporting)

    # --- nobody named a cause -------------------------------------------------
    if not substantive:
        return IncidentReport(
            incident_id=incident_id,
            created_at=created_at,
            status="insufficient_evidence",
            failure_type=FailureTaxonomy.UNKNOWN,
            confidence=min(
                MAX_UNKNOWN_CONFIDENCE,
                _clamp(max((finding.confidence for finding in findings), default=0.0)),
            ),
            summary=(
                f"No agent could explain incident {incident_id}. "
                + "; ".join(f"{f.agent}: {f.summary}" for f in findings)
            ),
            findings=findings,
            abstaining_agents=abstaining,
            missing_agents=missing,
            hypotheses=hypotheses,
            recommended_next_steps=(
                "Widen the investigation window or gather more evidence before proposing a fix.",
            ),
        )

    # --- group the agents that did name one -----------------------------------
    by_type: dict[FailureTaxonomy, list[AgentFinding]] = {}
    for finding in substantive:
        by_type.setdefault(finding.failure_type, []).append(finding)

    def strength(item: tuple[FailureTaxonomy, list[AgentFinding]]) -> tuple[int, float]:
        _, group = item
        return len(group), max(finding.confidence for finding in group)

    ranked = sorted(by_type.items(), key=strength, reverse=True)
    leading_type, leading_group = ranked[0]
    agreeing = tuple(finding.agent for finding in leading_group)

    confidence = max(finding.confidence for finding in leading_group)
    if len(leading_group) > 1:
        confidence += AGREEMENT_BONUS * (len(leading_group) - 1)

    conflicts = tuple(
        Conflict(
            failure_type=failure_type,
            claimed_by=tuple(finding.agent for finding in group),
            confidence=max(finding.confidence for finding in group),
        )
        for failure_type, group in ranked[1:]
    )
    if conflicts:
        confidence -= CONFLICT_PENALTY * len(conflicts)

    confidence = _clamp(confidence)

    # Several causes each backed by the same number of agents means none is actually
    # leading: confidence breaks the sort order, but it must not break the tie.
    leader_strength = max(finding.confidence for finding in leading_group)
    tied_groups = sum(
        1
        for _, group in ranked
        if len(group) == len(leading_group)
        and leader_strength - max(finding.confidence for finding in group) <= TIE_MARGIN
    )

    status: Literal["resolved_cause", "insufficient_evidence", "conflicting"]
    if confidence < MIN_REPORTABLE_CONFIDENCE:
        status = "insufficient_evidence"
    elif tied_groups > 1:
        status = "conflicting"
    else:
        status = "resolved_cause"

    # A conflicting report still names the strongest candidate, flagged as such; it is
    # never actionable. Only an insufficient report falls back to unknown.
    reported_type = FailureTaxonomy.UNKNOWN if status == "insufficient_evidence" else leading_type
    if reported_type is FailureTaxonomy.UNKNOWN:
        confidence = min(confidence, MAX_UNKNOWN_CONFIDENCE)

    agreement_note = (
        f"{len(agreeing)} agents agree ({', '.join(agreeing)})"
        if len(agreeing) > 1
        else f"only {agreeing[0]} names a cause"
    )
    conflict_note = (
        f" Disagreement: {'; '.join(conflict.describe() for conflict in conflicts)}."
        if conflicts
        else ""
    )
    abstain_note = f" {', '.join(abstaining)} found nothing in their evidence." if abstaining else ""
    missing_note = f" Incomplete: no evidence from {', '.join(missing)}." if missing else ""

    return IncidentReport(
        incident_id=incident_id,
        created_at=created_at,
        status=status,
        failure_type=reported_type,
        confidence=confidence,
        summary=(
            f"{leading_type.value} at {confidence:.2f} confidence - {agreement_note}."
            f"{conflict_note}{abstain_note}{missing_note}"
        ),
        findings=findings,
        agreeing_agents=agreeing,
        abstaining_agents=abstaining,
        missing_agents=missing,
        conflicts=conflicts,
        hypotheses=hypotheses,
        recommended_next_steps=_next_steps(documents, leading_type),
    )


__all__ = [
    "AGREEMENT_BONUS",
    "CONFLICT_PENALTY",
    "MAX_UNKNOWN_CONFIDENCE",
    "MIN_REPORTABLE_CONFIDENCE",
    "TIE_MARGIN",
    "AgentFinding",
    "Conflict",
    "IncidentReport",
    "synthesize",
]
