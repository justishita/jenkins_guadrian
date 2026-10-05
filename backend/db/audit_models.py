"""Audit-layer tables: what the agents did, what was proposed, and who decided.

**Owner:** P3. The audit layer is the traceability requirement of the project - every
agent action, tool call, policy verdict, human decision and remediation outcome must
be reconstructable after the fact, and the Grafana agent-ops dashboards read from
here. These tables are append-mostly on purpose: rows record that something happened,
they are not mutable state.

``backend.db.models`` holds the operational ``incidents`` table; this module only adds
tables that hang off it. DDL is owned by the Alembic migrations under ``migrations/``;
``Base.metadata.create_all`` still works for local development because the models are
imported by ``backend.db.models``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from backend.db.database import Base


# PostgreSQL gets JSONB for indexable payload queries; SQLite (tests) gets plain JSON.
JSON_PAYLOAD = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuditEventType(str, Enum):
    """Vocabulary for ``audit_events.event_type``.

    Extend deliberately: Grafana panels and evaluation scripts group on these values.
    """

    INCIDENT_CREATED = "incident_created"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    TOOL_CALL = "tool_call"
    LLM_CALL = "llm_call"
    EVIDENCE_WRITTEN = "evidence_written"
    HYPOTHESIS_FORMED = "hypothesis_formed"
    SYNTHESIS_COMPLETED = "synthesis_completed"
    POLICY_EVALUATED = "policy_evaluated"
    REMEDIATION_PROPOSED = "remediation_proposed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    PULL_REQUEST_CREATED = "pull_request_created"
    REVALIDATION_COMPLETED = "revalidation_completed"
    INCIDENT_LINKED = "incident_linked"


class AgentRunStatus(str, Enum):
    """Terminal states of one agent's work on one incident."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ProposalStatus(str, Enum):
    """Lifecycle of a proposed remediation, from draft to validated outcome."""

    DRAFT = "draft"
    POLICY_REJECTED = "policy_rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PR_CREATED = "pr_created"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"


class ApprovalDecision(str, Enum):
    """What a human reviewer decided about a proposal."""

    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class AuditEvent(Base):
    """Append-only trail of everything the system did for an incident.

    ``payload`` carries event-specific detail and must already be redacted by the
    writer; nothing in this table may contain a secret.
    """

    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), index=True, nullable=False)
    actor = Column(String(64), nullable=False)
    event_type = Column(String(64), nullable=False)
    summary = Column(Text, nullable=False, default="")
    payload = Column(JSON_PAYLOAD, nullable=False, default=dict)
    duration_ms = Column(Float, nullable=True)
    ok = Column(Boolean, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_audit_events_incident_created", "incident_id", "created_at"),
        Index("ix_audit_events_actor_type", "actor", "event_type"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "actor": self.actor,
            "event_type": self.event_type,
            "summary": self.summary,
            "payload": self.payload,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AgentRun(Base):
    """One agent's investigation of one incident, with the numbers we evaluate on.

    Investigation latency and root-cause accuracy are measured from these rows, so
    ``started_at``/``finished_at`` must be recorded even when the run fails.
    """

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), index=True, nullable=False)
    agent = Column(String(64), nullable=False)
    status = Column(String(32), default=AgentRunStatus.RUNNING.value, nullable=False)
    failure_type = Column(String(64), nullable=True)
    confidence = Column(Float, nullable=True)
    evidence_version = Column(Integer, nullable=True)
    tool_call_count = Column(Integer, default=0, nullable=False)
    llm_call_count = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Float, nullable=True)

    __table_args__ = (Index("ix_agent_runs_incident_agent", "incident_id", "agent"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "agent": self.agent,
            "status": self.status,
            "failure_type": self.failure_type,
            "confidence": self.confidence,
            "evidence_version": self.evidence_version,
            "tool_call_count": self.tool_call_count,
            "llm_call_count": self.llm_call_count,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
        }


class RemediationProposal(Base):
    """A minimal fix the Code & Remediation agent proposes for an incident.

    The diff is stored so a reviewer and the audit trail see exactly what was
    proposed, including for proposals that policy blocked and that never reached a
    pull request.
    """

    __tablename__ = "remediation_proposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), index=True, nullable=False)
    proposed_by = Column(String(64), nullable=False, default="code_agent")
    attempt = Column(Integer, default=0, nullable=False)
    status = Column(String(32), default=ProposalStatus.DRAFT.value, nullable=False)
    failure_type = Column(String(64), nullable=True)
    rationale = Column(Text, nullable=False, default="")
    diff = Column(Text, nullable=False, default="")
    changed_files = Column(JSON_PAYLOAD, nullable=False, default=list)
    confidence = Column(Float, nullable=True)
    pull_request_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "proposed_by": self.proposed_by,
            "attempt": self.attempt,
            "status": self.status,
            "failure_type": self.failure_type,
            "rationale": self.rationale,
            "diff": self.diff,
            "changed_files": self.changed_files,
            "confidence": self.confidence,
            "pull_request_url": self.pull_request_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PolicyDecision(Base):
    """The OPA/Conftest verdict on one proposal, kept whether it passed or failed.

    Blocked proposals are the interesting ones (TC-15), so the violations are stored
    rather than only a boolean.
    """

    __tablename__ = "policy_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(
        Integer, ForeignKey("remediation_proposals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    incident_id = Column(String(36), index=True, nullable=False)
    policy_package = Column(String(128), nullable=False)
    allowed = Column(Boolean, nullable=False)
    violations = Column(JSON_PAYLOAD, nullable=False, default=list)
    warnings = Column(JSON_PAYLOAD, nullable=False, default=list)
    evaluated_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "proposal_id": self.proposal_id,
            "incident_id": self.incident_id,
            "policy_package": self.policy_package,
            "allowed": self.allowed,
            "violations": self.violations,
            "warnings": self.warnings,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
        }


class Approval(Base):
    """The human decision on a proposal. No pull request may exist without one."""

    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(
        Integer, ForeignKey("remediation_proposals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    incident_id = Column(String(36), index=True, nullable=False)
    decision = Column(String(32), nullable=False)
    reviewer = Column(String(128), nullable=False)
    reason = Column(Text, nullable=False, default="")
    decided_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "proposal_id": self.proposal_id,
            "incident_id": self.incident_id,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }
