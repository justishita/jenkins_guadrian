"""Builders shared by the fusion tests and the coordinator scenarios."""

from datetime import datetime, timezone
from uuid import UUID

from common.models import Evidence, EvidenceHypothesis, EvidenceItem, FailureTaxonomy
from coordinator.agent import BlackboardView

INCIDENT = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
T = FailureTaxonomy


def make(
    agent: str,
    failure_type: FailureTaxonomy,
    confidence: float,
    *,
    status: str = "completed",
    coverage: float | None = None,
    contradicts: dict[FailureTaxonomy, list[str]] | None = None,
    steps: list[str] | None = None,
) -> Evidence:
    """One agent's evidence. ``contradicts`` adds hypotheses of other types citing evidence ids."""
    hypotheses = [
        EvidenceHypothesis(hypothesis=f"{agent} thinks {failure_type.value}", failure_type=failure_type, confidence=confidence)
    ]
    item_ids: list[str] = []
    for other, ids in (contradicts or {}).items():
        item_ids += ids
        hypotheses.append(
            EvidenceHypothesis(
                hypothesis=f"{agent} checked {other.value}", failure_type=other, confidence=0.1, contradicting_evidence=ids
            )
        )
    items = [
        EvidenceItem(id=i, kind="metric", source="test", content="{}") for i in dict.fromkeys(item_ids)
    ]
    return Evidence(
        incident_id=INCIDENT,
        agent=agent,  # type: ignore[arg-type]
        created_at=NOW,
        status=status,  # type: ignore[arg-type]
        failure_type=failure_type,
        summary=f"{agent} summary",
        root_cause_hypotheses=hypotheses,
        evidence_items=items,
        confidence=confidence,
        recommended_next_steps=steps or [],
        redaction_applied=True,
        investigation_coverage=coverage,
    )


def view(*evidence: Evidence) -> BlackboardView:
    return BlackboardView(incident_id=INCIDENT, evidence=tuple(evidence))
