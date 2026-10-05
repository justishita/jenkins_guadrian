"""Audit trail for the Metrics agent, written through the shared `common.audit` layer.

One investigation produces, in order: `agent_started`, a `tool_call` record per
Prometheus query, `hypothesis_formed`, `evidence_written`, and `agent_completed`
(or `agent_failed`). The first and last come from `AuditLog.agent_run`; the rest are
derived from the finished evidence, so the trail always agrees with what was stored.
"""

from datetime import datetime
from typing import Any

from common.audit import AuditLog, AuditRecord
from common.models import Evidence

from .evidence import AGENT_NAME


def _issued_at(args: dict[str, Any]) -> datetime | None:
    """When a query was issued, from `issued_at` in the tool-call args (absent on old evidence)."""
    value = args.get("issued_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def tool_call_records(evidence: Evidence) -> list[AuditRecord]:
    """One record per query, stamped with the time the query was issued, not written."""
    records: list[AuditRecord] = []
    for call in evidence.tool_calls:
        extra: dict[str, Any] = {}
        issued_at = _issued_at(call.args)
        if issued_at is not None:
            extra["created_at"] = issued_at
        records.append(
            AuditRecord(
                incident_id=evidence.incident_id,
                actor=AGENT_NAME,
                event_type="tool_call",
                summary=call.tool,
                payload={"args": call.args},
                duration_ms=call.duration_ms,
                ok=call.ok,
                **extra,
            )
        )
    return records


def hypothesis_record(evidence: Evidence) -> AuditRecord:
    return AuditRecord(
        incident_id=evidence.incident_id,
        actor=AGENT_NAME,
        event_type="hypothesis_formed",
        summary=evidence.summary,
        payload={
            "failure_type": evidence.failure_type.value,
            "confidence": evidence.confidence,
            "hypotheses": [
                {
                    "failure_type": hypothesis.failure_type.value,
                    "confidence": hypothesis.confidence,
                    "supporting_evidence": hypothesis.supporting_evidence,
                    "contradicting_evidence": hypothesis.contradicting_evidence,
                }
                for hypothesis in evidence.root_cause_hypotheses
            ],
        },
        ok=True,
    )


def evidence_written_record(evidence: Evidence) -> AuditRecord:
    return AuditRecord(
        incident_id=evidence.incident_id,
        actor=AGENT_NAME,
        event_type="evidence_written",
        summary=evidence.summary,
        payload={
            "status": evidence.status,
            "failure_type": evidence.failure_type.value,
            "confidence": evidence.confidence,
            "evidence_items": len(evidence.evidence_items),
        },
        ok=True,
    )


async def record_findings(audit: AuditLog, evidence: Evidence) -> None:
    """Append the per-query, hypothesis and evidence records for a finished investigation."""
    for record in tool_call_records(evidence):
        await audit.record(record)
    await audit.record(hypothesis_record(evidence))
    await audit.record(evidence_written_record(evidence))


def completion_details(evidence: Evidence) -> dict[str, object]:
    """Payload for the `agent_completed` record."""
    return {
        "status": evidence.status,
        "failure_type": evidence.failure_type.value,
        "confidence": evidence.confidence,
        "tool_calls": len(evidence.tool_calls),
    }
