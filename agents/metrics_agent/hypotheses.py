"""Turn per-query findings into root-cause hypotheses and an overall verdict."""

from dataclasses import dataclass, field
from typing import Literal

from common.models import EvidenceHypothesis, FailureTaxonomy
from common.redaction import redact

from .anomaly import Detection
from .queries import QuerySpec
from .sanity import SanityIssue

# Confidence for "metrics looked normal": we learned something, but not the cause.
NORMAL_METRICS_CONFIDENCE = 0.2

_NEXT_STEPS: dict[FailureTaxonomy, str] = {
    FailureTaxonomy.TIMEOUT: "Check slow endpoints and upstream dependencies for the latency spike window.",
    FailureTaxonomy.RESOURCE_EXHAUSTION: "Inspect CPU/memory limits and recent code paths that allocate or loop.",
    FailureTaxonomy.INFRA_NETWORK_FAILURE: "Verify the target service was running and reachable during the window.",
}


@dataclass
class Finding:
    """Outcome of one catalog query. Exactly one of detection / issues / error explains it."""

    spec: QuerySpec
    detection: Detection | None = None
    issues: list[SanityIssue] = field(default_factory=list)
    error: str | None = None

    @property
    def evidence_id(self) -> str:
        return f"metric-{self.spec.name}"


@dataclass
class Synthesis:
    status: Literal["completed", "insufficient_evidence"]
    failure_type: FailureTaxonomy
    confidence: float
    summary: str
    hypotheses: list[EvidenceHypothesis]
    next_steps: list[str]


def synthesize(findings: list[Finding]) -> Synthesis:
    """Pick the strongest anomaly, or honestly report that metrics do not explain the failure."""
    anomalous = [f for f in findings if f.detection and f.detection.anomalous]
    if anomalous:
        anomalous.sort(key=lambda f: f.detection.confidence, reverse=True)  # type: ignore[union-attr]
        hypotheses = [_hypothesis(f) for f in anomalous]
        top = hypotheses[0]
        return Synthesis(
            status="completed",
            failure_type=top.failure_type,
            confidence=top.confidence,
            summary=redact(f"Metrics anomaly: {top.hypothesis}"),
            hypotheses=hypotheses,
            next_steps=[s for s in dict.fromkeys(_NEXT_STEPS.get(h.failure_type, "") for h in hypotheses) if s],
        )

    usable = [f for f in findings if f.detection is not None]
    confidence = NORMAL_METRICS_CONFIDENCE if usable else 0.0
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


def _hypothesis(finding: Finding) -> EvidenceHypothesis:
    detection = finding.detection
    assert detection is not None
    text = f"{finding.spec.name}: {detection.detail}"
    return EvidenceHypothesis(
        hypothesis=redact(text),
        failure_type=finding.spec.failure_type,
        confidence=detection.confidence,
        supporting_evidence=[finding.evidence_id],
    )
