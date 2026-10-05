"""Turn per-metric findings into root-cause hypotheses and an overall verdict.

Per-metric detection has already happened. `correlation.correlate` groups the findings into
candidate causes; this module scores each one (`confidence.py`), writes the reasoning into
the hypothesis, and ranks them. The final evidence therefore carries the cross-metric
reasoning: what supports a cause, what contradicts it, and why.
"""

from dataclasses import dataclass
from typing import Literal

from common.models import EvidenceHypothesis, FailureTaxonomy
from common.redaction import redact

from .confidence import (
    base_confidence,
    final_confidence,
    inputs_from,
    normal_confidence,
)
from .correlation import CandidateCause, coincide, correlate
from .findings import Finding

__all__ = ["Finding", "Synthesis", "synthesize"]

_NEXT_STEPS: dict[FailureTaxonomy, str] = {
    FailureTaxonomy.TIMEOUT: "Check slow endpoints and upstream dependencies for the latency spike window.",
    FailureTaxonomy.RESOURCE_EXHAUSTION: "Inspect CPU/memory limits and recent code paths that allocate or loop.",
    FailureTaxonomy.INFRA_NETWORK_FAILURE: "Verify the target service was running and reachable during the window.",
}


@dataclass
class Synthesis:
    status: Literal["completed", "insufficient_evidence"]
    failure_type: FailureTaxonomy
    confidence: float
    summary: str
    hypotheses: list[EvidenceHypothesis]
    next_steps: list[str]


def synthesize(findings: list[Finding]) -> Synthesis:
    """Rank the candidate causes, or honestly report that metrics do not explain the failure."""
    hypotheses = sorted((_hypothesis(cause) for cause in correlate(findings)), key=lambda h: h.confidence, reverse=True)
    if hypotheses:
        top = hypotheses[0]
        return Synthesis(
            status="completed",
            failure_type=top.failure_type,
            confidence=top.confidence,
            summary=redact(f"Metrics anomaly: {top.hypothesis}"),
            hypotheses=hypotheses,
            next_steps=[s for s in dict.fromkeys(_NEXT_STEPS.get(h.failure_type, "") for h in hypotheses) if s],
        )
    return _nothing_found(findings)


def _nothing_found(findings: list[Finding]) -> Synthesis:
    usable = [f for f in findings if f.detection is not None]
    confidence = normal_confidence([f.quality for f in usable])
    reason = (
        f"{len(usable)} metric(s) checked, none anomalous"
        if usable
        else "no usable metric data in the investigation window"
    )
    return Synthesis(
        status="insufficient_evidence",
        failure_type=FailureTaxonomy.UNKNOWN,
        confidence=confidence,
        summary=f"Metrics do not explain this failure: {reason}.",
        hypotheses=[
            EvidenceHypothesis(
                hypothesis=f"No metric anomaly found ({reason}).",
                failure_type=FailureTaxonomy.UNKNOWN,
                confidence=confidence,
                supporting_evidence=[f.evidence_id for f in findings],
            )
        ],
        next_steps=["Rely on Jenkins log and code-change evidence; metrics show no resource or latency cause."],
    )


def _names(findings: list[Finding]) -> str:
    return ", ".join(f.spec.name for f in findings)


def _hypothesis(cause: CandidateCause) -> EvidenceHypothesis:
    scored = [(f, inputs_from(f.detection, f.quality)) for f in cause.primaries]  # type: ignore[arg-type]
    best, best_inputs = max(scored, key=lambda pair: base_confidence(pair[1]))
    others = [f for f in cause.primaries if f is not best]
    # Only anomalies at the same time corroborate; an earlier one is cited but does not add confidence.
    together = [f for f in others if coincide(best, f)]
    earlier = [f for f in others if f not in together]
    corroborating = len(together) + len(cause.symptoms)

    text = f"{best.spec.name}: {best.detection.detail}"  # type: ignore[union-attr]
    if together:
        text += f"; also anomalous: {_names(together)}"
    if earlier:
        text += f"; separate earlier anomaly: {_names(earlier)}"
    if cause.symptoms:
        text += f"; coincides with elevated {_names(cause.symptoms)} (a likely symptom)"
    if cause.contradicting:
        text += f"; {_names(cause.contradicting)} stayed normal"
    if cause.unreported:
        text += f"; {len(cause.unreported)} other metric(s) stopped reporting, consistent with the outage"

    return EvidenceHypothesis(
        hypothesis=redact(text),
        failure_type=cause.failure_type,
        confidence=final_confidence(best_inputs, corroborating, len(cause.contradicting)),
        supporting_evidence=[f.evidence_id for f in [best, *others, *cause.symptoms]],
        contradicting_evidence=[f.evidence_id for f in cause.contradicting],
    )
