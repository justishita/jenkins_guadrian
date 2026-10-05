"""Shared models and taxonomy generated from the evidence contract."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FailureTaxonomy(str, Enum):
	CODE_TEST_FAILURE = "code_test_failure"
	BUILD_COMPILATION_FAILURE = "build_compilation_failure"
	DEPENDENCY_REGRESSION = "dependency_regression"
	TIMEOUT = "timeout"
	RESOURCE_EXHAUSTION = "resource_exhaustion"
	INFRA_NETWORK_FAILURE = "infra_network_failure"
	CONFIG_ERROR = "config_error"
	AUTH_FAILURE = "auth_failure"
	FLAKY_TEST = "flaky_test"
	DEPLOYMENT_FAILURE = "deployment_failure"
	UNKNOWN = "unknown"


class IncidentCreatedEvent(BaseModel):
	"""Validated message published for a newly created incident."""

	model_config = ConfigDict(extra="ignore")

	incident_id: UUID
	job_name: str = Field(min_length=1)
	build_number: int = Field(ge=0)
	build_url: str = Field(min_length=1)
	branch: str = Field(min_length=1)
	git_commit: str = Field(min_length=1)
	failed_stage: str | None = None
	timestamp: datetime
	remediation_attempt: int = Field(default=0, ge=0)

	@field_validator("timestamp")
	@classmethod
	def ensure_utc(cls, value: datetime) -> datetime:
		if value.tzinfo is None:
			return value.replace(tzinfo=timezone.utc)
		return value.astimezone(timezone.utc)


class EvidenceLocation(BaseModel):
	file: str | None = None
	line: int | None = None


class EvidenceItem(BaseModel):
	id: str = Field(min_length=1)
	kind: Literal["log_excerpt", "metric", "commit", "test_result"]
	source: str = Field(min_length=1)
	timestamp: datetime | None = None
	content: str
	location: EvidenceLocation | None = None


class EvidenceHypothesis(BaseModel):
	hypothesis: str = Field(min_length=1)
	failure_type: FailureTaxonomy
	confidence: float = Field(ge=0, le=1)
	supporting_evidence: list[str] = Field(default_factory=list)
	contradicting_evidence: list[str] = Field(default_factory=list)


class ToolCallRecord(BaseModel):
	tool: str = Field(min_length=1)
	args: dict[str, Any] = Field(default_factory=dict)
	duration_ms: float = Field(ge=0)
	ok: bool


class Evidence(BaseModel):
	"""Versioned Jenkins-agent evidence, aligned with the checked-in stub schema."""

	schema_version: Literal["0.1-stub"] = "0.1-stub"
	incident_id: UUID
	agent: Literal["jenkins_agent", "metrics_agent", "code_agent"] = "jenkins_agent"
	created_at: datetime
	status: Literal["completed", "failed", "insufficient_evidence"]
	failure_type: FailureTaxonomy
	summary: str = Field(min_length=1)
	root_cause_hypotheses: list[EvidenceHypothesis] = Field(default_factory=list)
	evidence_items: list[EvidenceItem] = Field(default_factory=list)
	tool_calls: list[ToolCallRecord] = Field(default_factory=list)
	confidence: float = Field(ge=0, le=1)
	recommended_next_steps: list[str] = Field(default_factory=list)
	failed_stage: str | None = None
	failing_tests: list[str] = Field(default_factory=list)
	error_signature: str = ""
	log_truncated: bool = False
	redaction_applied: bool = False
	flaky_score: float | None = Field(default=None, ge=0, le=1)

	@field_validator("created_at")
	@classmethod
	def ensure_created_at_utc(cls, value: datetime) -> datetime:
		if value.tzinfo is None:
			return value.replace(tzinfo=timezone.utc)
		return value.astimezone(timezone.utc)

	@model_validator(mode="after")
	def validate_evidence_citations(self) -> "Evidence":
		evidence_ids = {item.id for item in self.evidence_items}
		for hypothesis in self.root_cause_hypotheses:
			cited_ids = set(hypothesis.supporting_evidence + hypothesis.contradicting_evidence)
			unknown_ids = cited_ids - evidence_ids
			if unknown_ids:
				raise ValueError(f"hypothesis cites unknown evidence IDs: {sorted(unknown_ids)}")
		return self