"""Evidence record built by the Metrics agent, shaped like common/evidence_schema_stub.json."""

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from common.models import FailureTaxonomy
from common.redaction import redact

SCHEMA_VERSION = "0.1-stub"
AGENT_NAME = "metrics_agent"


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
    # Every free-text field is passed through common.redaction.redact() when it is built.
    redaction_applied: bool = True

    @field_validator("failed_stage")
    @classmethod
    def _redact_failed_stage(cls, value: str | None) -> str | None:
        # failed_stage comes from the incoming event, so treat it as untrusted free text.
        return redact(value) if value else value

    def to_json_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict (UTC ISO-8601 timestamps) for schema validation and storage."""
        return self.model_dump(mode="json", exclude_none=True)


def item_content(payload: dict[str, Any]) -> str:
    """Serialise evidence details deterministically, redacted."""
    return redact(json.dumps(payload, sort_keys=True, default=str))
