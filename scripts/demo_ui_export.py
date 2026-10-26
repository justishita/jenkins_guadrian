"""Write the P3 demo's results where the web UI reads them.

**Owner:** P3. ``scripts/demo_p3.py --to-ui`` runs the same five scenarios and then calls
this module, so each one can be opened, inspected and decided in the UI:

* the ``incidents``, ``remediation_proposals``, ``policy_decisions``, ``approvals`` and
  ``audit_events`` rows, in the database at ``DATABASE_URL``;
* the agents' evidence on the blackboard (``BLACKBOARD_DIR``), where the review page
  reads it;
* the Coordinator's report as a revision under ``REPORTS_DIR``, exactly as the live
  Coordinator stores it.

Only this module's fixed demo incident ids are ever deleted or rewritten, so running it
against a database that also holds real incidents leaves those alone. Nothing here
publishes to RabbitMQ or touches GitHub.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agents.code_agent.remediation import FixPlan
from backend.db.audit_models import (
    Approval,
    AuditEvent,
    PolicyDecision,
    ProposalStatus,
    RemediationProposal,
)
from backend.db.database import Base
from backend.db.models import Incident
from backend.db.ui_models import Project
from common.audit import AuditRecord
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import Evidence, IncidentCreatedEvent
from coordinator.remediation_gate import RemediationRecord
from coordinator.revisions import RevisionStore, evidence_fingerprint
from coordinator.synthesis import IncidentReport


PROJECT_NAME = "target-app"
PROJECT_JOB = "target-app"

# RemediationRecord.status -> remediation_proposals.status
_PROPOSAL_STATUS = {
    "policy_rejected": ProposalStatus.POLICY_REJECTED.value,
    "awaiting_approval": ProposalStatus.AWAITING_APPROVAL.value,
    "rejected": ProposalStatus.REJECTED.value,
    "pr_blocked": ProposalStatus.APPROVED.value,
    "pr_created": ProposalStatus.PR_CREATED.value,
    "failed": ProposalStatus.APPROVED.value,
}


def diff_text(plan: FixPlan) -> str:
    """The plan as a unified diff, one hunk per edit, as the review page renders it."""
    lines: list[str] = []
    for path in plan.changed_paths:
        lines += [f"--- a/{path}", f"+++ b/{path}"]
        for edit in (e for e in plan.edits if e.path == path):
            lines += ["@@", f"-{edit.before}", f"+{edit.after}"]
    return "\n".join(lines) + "\n"


class UiExport:
    """Collects nothing; each call writes one piece of one scenario straight away."""

    def __init__(self, database_url: str, reports_dir: Path, blackboard_dir: Path) -> None:
        self._engine = create_async_engine(database_url)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False, class_=AsyncSession)
        self._reports = RevisionStore(reports_dir)
        self._blackboard = FileBlackboard(blackboard_dir)
        self._clock = datetime.now(timezone.utc) - timedelta(hours=2)

    def _tick(self) -> datetime:
        """Monotonic timestamps, so lists and timelines read in scenario order."""
        self._clock += timedelta(seconds=7)
        return self._clock

    async def prepare(self, incident_ids: Iterable[UUID | str]) -> None:
        """Create the tables if needed, clear previous demo rows, and register the project."""
        ids = [str(i) for i in incident_ids]
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self._sessions() as session:
            for model in (Approval, PolicyDecision, RemediationProposal, AuditEvent):
                await session.execute(delete(model).where(model.incident_id.in_(ids)))
            # Clear the links first: incidents reference each other.
            for incident in (await session.execute(select(Incident).where(Incident.id.in_(ids)))).scalars():
                incident.related_incident_id = None
            await session.flush()
            await session.execute(delete(Incident).where(Incident.id.in_(ids)))
            existing = (
                await session.execute(select(Project).where(Project.name == PROJECT_NAME))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Project(
                        name=PROJECT_NAME,
                        description="Demo target application (scripts/demo_p3.py --to-ui)",
                        jenkins_job=PROJECT_JOB,
                        github_repo="jenkinsguardians/target-app",
                        prometheus_job="target-app",
                    )
                )
            await session.commit()

    async def incident(
        self,
        event: IncidentCreatedEvent,
        *,
        build_number: int,
        status: str = "OPEN",
        related_incident_id: str | None = None,
    ) -> None:
        async with self._sessions() as session:
            session.add(
                Incident(
                    id=str(event.incident_id),
                    job_name=event.job_name,
                    build_number=build_number,
                    build_url=event.build_url.replace("/42/", f"/{build_number}/"),
                    branch=event.branch,
                    git_commit=event.git_commit,
                    failed_stage=event.failed_stage,
                    status=status,
                    event_key=f"demo-{event.incident_id}",
                    remediation_attempt=event.remediation_attempt,
                    related_incident_id=related_incident_id,
                    created_at=self._tick(),
                )
            )
            await session.commit()

    async def evidence(self, evidence: Iterable[Evidence]) -> None:
        """Post each agent's document to the blackboard, as the live agents do."""
        for item in evidence:
            self._blackboard.post(
                BlackboardEntry(
                    incident_id=item.incident_id,
                    section="evidence",
                    key=item.agent,
                    author=item.agent,
                    payload=item.model_dump(mode="json"),
                )
            )

    async def report(self, report: IncidentReport, evidence: list[Evidence]) -> None:
        await self._reports.persist(
            report, attempt=0, fingerprint=evidence_fingerprint(evidence), reason="initial", now=self._tick()
        )

    async def audit(self, records: Iterable[AuditRecord], *, skip: set[str] = frozenset()) -> None:
        """Copy the demo's audit trail into ``audit_events``, the table the timeline reads."""
        async with self._sessions() as session:
            for record in records:
                if record.event_type in skip:
                    continue
                session.add(
                    AuditEvent(
                        incident_id=str(record.incident_id),
                        actor=record.actor,
                        event_type=record.event_type,
                        summary=record.summary,
                        payload=record.payload,
                        duration_ms=record.duration_ms,
                        ok=record.ok,
                        created_at=self._tick(),
                    )
                )
            await session.commit()

    async def event(self, incident_id: UUID, actor: str, event_type: str, summary: str, *, ok: bool) -> None:
        await self.audit([AuditRecord(incident_id=incident_id, actor=actor, event_type=event_type, summary=summary, ok=ok)])

    async def proposal(
        self,
        plan: FixPlan,
        record: RemediationRecord,
        *,
        pull_request_policy: bool = False,
    ) -> None:
        """The fix, the verdicts it got, and - if one was made - the human decision."""
        async with self._sessions() as session:
            proposal = RemediationProposal(
                incident_id=str(plan.incident_id),
                proposed_by="code_agent",
                attempt=plan.attempt,
                status=_PROPOSAL_STATUS[record.status],
                failure_type=plan.failure_type.value,
                rationale=plan.rationale,
                diff=diff_text(plan),
                changed_files=list(plan.changed_paths),
                confidence=plan.confidence,
                # GitHub is simulated in the demo, so there is no real pull request to link.
                pull_request_url=None,
                created_at=self._tick(),
            )
            session.add(proposal)
            await session.flush()
            session.add(
                PolicyDecision(
                    proposal_id=proposal.id,
                    incident_id=str(plan.incident_id),
                    policy_package="jenkinsguardians/remediation",
                    allowed=record.status != "policy_rejected",
                    violations=list(record.violations) if record.status == "policy_rejected" else [],
                    warnings=list(record.warnings),
                    evaluated_at=self._tick(),
                )
            )
            if pull_request_policy:
                session.add(
                    PolicyDecision(
                        proposal_id=proposal.id,
                        incident_id=str(plan.incident_id),
                        policy_package="jenkinsguardians/pullrequest",
                        allowed=True,
                        violations=[],
                        warnings=[],
                        evaluated_at=self._tick(),
                    )
                )
            if record.approval is not None:
                session.add(
                    Approval(
                        proposal_id=proposal.id,
                        incident_id=str(plan.incident_id),
                        attempt=plan.attempt,
                        decision=record.approval.decision,
                        reviewer=record.approval.reviewer,
                        comment=record.approval.reason,
                        decided_at=self._tick(),
                    )
                )
            await session.commit()

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = ["UiExport", "diff_text"]
