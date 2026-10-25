"""collect -> fuse -> store -> audit, for the richer ``FusedReport`` (P2).

P3's ``CoordinatorAgent.synthesize`` (``coordinator.synthesis``) stays the report the
remediation engine and policy gate read. This module is the second, more detailed fusion:
relevance-weighted scoring, gated negative evidence, and separate completeness and conflict
fields. It reads the blackboard and never alters it. The two fusions are meant to be compared
(see ``docs/fusion_method_comparison.md``) before either is made authoritative.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID

from common.audit import AuditLog, AuditRecord
from common.evidence_store import EvidenceStore
from coordinator.agent import BlackboardView
from coordinator.fusion import FusionConfig, fuse
from coordinator.report import FusedReport
from coordinator.report_store import ReportStore

ACTOR = "coordinator"


async def synthesize_fused(
    store: EvidenceStore,
    incident_id: UUID | str,
    *,
    report_store: ReportStore | None = None,
    audit: AuditLog | None = None,
    config: FusionConfig | None = None,
    now: datetime | None = None,
) -> FusedReport:
    """Fuse whatever is on the blackboard for one incident; store and audit the result."""
    started = time.perf_counter()
    identifier = UUID(str(incident_id))
    view = BlackboardView(incident_id=identifier, evidence=tuple(await store.read_all(identifier)))
    report = fuse(view, config, now=now or datetime.now(timezone.utc))
    if report_store is not None:
        await report_store.write(report)
    if audit is not None:
        await audit.record(
            AuditRecord(
                incident_id=report.incident_id,
                actor=ACTOR,
                event_type="synthesis_completed",
                summary=report.narrative,
                payload={
                    "verdict": report.verdict.value,
                    "confidence": report.confidence,
                    "completeness": report.completeness.value,
                    "conflict_status": report.conflict_status.value,
                    "fusion_method": report.fusion_method.value,
                    "missing_agents": report.missing_agents,
                    "failed_agents": report.failed_agents,
                    "ready_for_policy_review": report.ready_for_policy_review,
                },
                duration_ms=(time.perf_counter() - started) * 1000,
                ok=True,
            )
        )
    return report


__all__ = ["synthesize_fused"]
