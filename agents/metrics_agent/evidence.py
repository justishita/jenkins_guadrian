"""Evidence record built by the Metrics agent, shaped like common/evidence_schema_stub.json."""

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .taxonomy import FailureTaxonomy

SCHEMA_VERSION = "0.1-stub"
AGENT_NAME = "metrics_agent"

try:  # P3 owns the real implementation; until it exists we cannot claim redaction ran.
    from common.redaction import redact as _redact

    REDACTION_AVAILABLE = True
except ImportError:
    REDACTION_AVAILABLE = False

    def _redact(text: str) -> str:
        return text


def redact(text: str) -> str:
    """Pass free text through the shared redactor before it is stored."""
    return _redact(text)


class RootCauseHypothesis(BaseModel):
    hypothesis: str
    failure_type: FailureTaxonomy
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    id: str
    kind: Literal["metric"] = "metric"
    source: str = "prometheus"
    timestamp: datetime | None = None
    content: str


class ToolCall(BaseModel):
    tool: str
    args: dict[str, Any]
    duration_ms: float
    ok: bool


class EvidenceRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    incident_id: UUID
    agent: Literal["metrics_agent"] = AGENT_NAME
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Literal["completed", "failed", "insufficient_evidence"]
    failure_type: FailureTaxonomy
    summary: str
    root_cause_hypotheses: list[RootCauseHypothesis]
    evidence_items: list[EvidenceItem]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_next_steps: list[str] = Field(default_factory=list)
    failed_stage: str | None = None
    redaction_applied: bool = REDACTION_AVAILABLE

    def to_json_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict (UTC ISO-8601 timestamps) for schema validation and storage."""
        return self.model_dump(mode="json", exclude_none=True)


def item_content(payload: dict[str, Any]) -> str:
    """Serialise evidence details deterministically, redacted."""
    return redact(json.dumps(payload, sort_keys=True, default=str))
