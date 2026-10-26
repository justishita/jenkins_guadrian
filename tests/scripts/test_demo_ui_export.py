"""Tests for ``scripts/demo_ui_export.py``: the demo's results, as the web UI reads them.

**Owner:** P3. The UI reads the same tables the live system writes, so what matters is
that the export lands in those tables with the states the review page acts on, and that
re-running it touches only the demo's own incidents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from agents.code_agent.remediation import FileEdit, FixPlan
from backend.db.audit_models import Approval, PolicyDecision, RemediationProposal
from backend.db.models import Incident
from backend.db.ui_models import Project
from common.fix_branch import format_fix_branch
from common.models import FailureTaxonomy, IncidentCreatedEvent
from coordinator.remediation_gate import Approval as Decision, RemediationRecord
from scripts.demo_ui_export import UiExport, diff_text


DEMO_ID = UUID("11111111-0303-4303-8303-000000000003")


def plan(incident_id: UUID = DEMO_ID) -> FixPlan:
    return FixPlan(
        incident_id=incident_id,
        failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION,
        confidence=0.9,
        base_ref="a1b2c3d4",
        branch_name=format_fix_branch(incident_id, 0),
        commit_message="Pin httpx back",
        rationale="Revert the bump.",
        edits=(FileEdit(path="target_app/requirements.txt", before="httpx==0.99.0", after="httpx==0.28.1"),),
    )


def event(incident_id: UUID = DEMO_ID) -> IncidentCreatedEvent:
    return IncidentCreatedEvent(
        incident_id=incident_id,
        job_name="target-app/main",
        build_number=42,
        build_url="http://jenkins:8080/job/target-app/job/main/42/",
        branch="main",
        git_commit="a1b2c3d4",
        failed_stage="Setup",
        timestamp=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )


@pytest.fixture
def export(tmp_path: Path):
    path = (tmp_path / "ui.db").as_posix()
    exporter = UiExport(f"sqlite+aiosqlite:///{path}", tmp_path / "reports", tmp_path / "blackboard")
    yield exporter, create_engine(f"sqlite:///{path}")


def test_diff_text_is_a_unified_diff_per_file() -> None:
    assert diff_text(plan()) == (
        "--- a/target_app/requirements.txt\n+++ b/target_app/requirements.txt\n@@\n-httpx==0.99.0\n+httpx==0.28.1\n"
    )


@pytest.mark.asyncio
async def test_a_pending_fix_is_left_for_a_reviewer(export) -> None:
    exporter, engine = export
    await exporter.prepare([DEMO_ID])
    await exporter.incident(event(), build_number=45)
    record = RemediationRecord(
        incident_id=DEMO_ID, branch_name=plan().branch_name, attempt=0, status="awaiting_approval", policy_allowed=True
    )
    await exporter.proposal(plan(), record)
    await exporter.close()

    with Session(engine) as session:
        proposal = session.execute(select(RemediationProposal)).scalar_one()
        assert proposal.status == "awaiting_approval"
        assert proposal.diff.startswith("--- a/target_app/requirements.txt")
        assert session.execute(select(PolicyDecision.allowed)).scalar_one() is True
        assert session.execute(select(Approval)).first() is None
        assert session.execute(select(Project.jenkins_job)).scalar_one() == "target-app"


@pytest.mark.asyncio
async def test_a_blocked_fix_keeps_its_violations(export) -> None:
    exporter, engine = export
    await exporter.prepare([DEMO_ID])
    await exporter.incident(event(), build_number=43)
    record = RemediationRecord(
        incident_id=DEMO_ID, branch_name=plan().branch_name, attempt=0, status="policy_rejected",
        violations=("proposal disables a test",),
    )
    await exporter.proposal(plan(), record)
    await exporter.close()

    with Session(engine) as session:
        assert session.execute(select(RemediationProposal.status)).scalar_one() == "policy_rejected"
        decision = session.execute(select(PolicyDecision)).scalar_one()
        assert decision.allowed is False
        assert decision.violations == ["proposal disables a test"]


@pytest.mark.asyncio
async def test_a_decided_fix_records_the_reviewer(export) -> None:
    exporter, engine = export
    await exporter.prepare([DEMO_ID])
    await exporter.incident(event(), build_number=42)
    record = RemediationRecord(
        incident_id=DEMO_ID, branch_name=plan().branch_name, attempt=0, status="pr_created", policy_allowed=True,
        approval=Decision(decision="approved", reviewer="ishita", reason="Pin back."),
    )
    await exporter.proposal(plan(), record, pull_request_policy=True)
    await exporter.close()

    with Session(engine) as session:
        approval = session.execute(select(Approval)).scalar_one()
        assert (approval.decision, approval.reviewer, approval.comment) == ("approved", "ishita", "Pin back.")
        packages = set(session.execute(select(PolicyDecision.policy_package)).scalars())
        assert packages == {"jenkinsguardians/remediation", "jenkinsguardians/pullrequest"}


@pytest.mark.asyncio
async def test_rerunning_replaces_demo_rows_and_leaves_others_alone(export) -> None:
    exporter, engine = export
    other = uuid4()
    await exporter.prepare([DEMO_ID])
    await exporter.incident(event(other), build_number=7)
    await exporter.incident(event(), build_number=42)
    await exporter.prepare([DEMO_ID])  # a second run
    await exporter.incident(event(), build_number=42)
    await exporter.close()

    with Session(engine) as session:
        ids = set(session.execute(select(Incident.id)).scalars())
        assert ids == {str(other), str(DEMO_ID)}
        assert len(session.execute(select(Project)).scalars().all()) == 1
