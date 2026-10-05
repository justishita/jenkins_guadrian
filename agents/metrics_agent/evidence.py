"""Builders for the shared `common.models.Evidence` record.

The contract itself (fields, taxonomy, citation checks) lives in `common/` and is
owned jointly; this module only fills it in for the Metrics agent.
"""

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from common.models import (
    Evidence,
    EvidenceHypothesis,
    EvidenceItem,
    FailureTaxonomy,
    ToolCallRecord,
)
from common.redaction import redact

AGENT_NAME = "metrics_agent"
METRIC_SOURCE = "prometheus"

Status = Literal["completed", "failed", "insufficient_evidence"]


def item_content(payload: dict[str, Any]) -> str:
    """Serialise evidence details deterministically, redacted."""
    return redact(json.dumps(payload, sort_keys=True, default=str))


def metric_item(item_id: str, timestamp: datetime, payload: dict[str, Any]) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        kind="metric",
        source=METRIC_SOURCE,
        timestamp=timestamp,
        content=item_content(payload),
    )


def make_evidence(
    *,
    incident_id: UUID,
    status: Status,
    failure_type: FailureTaxonomy,
    summary: str,
    confidence: float,
    hypotheses: list[EvidenceHypothesis] | None = None,
    items: list[EvidenceItem] | None = None,
    tool_calls: list[ToolCallRecord] | None = None,
    next_steps: list[str] | None = None,
    failed_stage: str | None = None,
    created_at: datetime | None = None,
) -> Evidence:
    """Build the Metrics agent's evidence, redacting every free-text field.

    `failed_stage` comes from the incoming event, so it is treated as untrusted text.
    """
    return Evidence(
        incident_id=incident_id,
        agent=AGENT_NAME,
        created_at=created_at or datetime.now(timezone.utc),
        status=status,
        failure_type=failure_type,
        summary=redact(summary),
        root_cause_hypotheses=hypotheses or [],
        evidence_items=items or [],
        tool_calls=tool_calls or [],
        confidence=confidence,
        recommended_next_steps=[redact(step) for step in next_steps or []],
        failed_stage=redact(failed_stage) if failed_stage else None,
        redaction_applied=True,
    )
