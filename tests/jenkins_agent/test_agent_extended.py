"""Extended unit tests for agents/jenkins_agent/agent.py."""

import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

import agents.jenkins_agent.agent as agent_module
from agents.jenkins_agent.agent import (
    EvidenceDraft,
    JenkinsInvestigationAgent,
    _fallback_evidence,
    _initial_evidence_items,
    _load_prompt_variant,
    run_investigation,
)
from agents.jenkins_agent.parsing import CompileError, FailingTest, ParsedBuild
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import EvidenceHypothesis, FailureTaxonomy, IncidentCreatedEvent


def test_load_prompt_variant_fallback():
    """Missing or invalid variant should fall back safely to defaults."""
    data = _load_prompt_variant("non_existent_variant_12345")
    assert "role" in data
    assert "goal" in data


def test_initial_evidence_items_with_compiler_errors():
    event = IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="target-app/main",
        build_number=15,
        build_url="http://jenkins:8080/job/target-app/job/main/15/",
        branch="main",
        git_commit="abc1234",
        timestamp=datetime.now(timezone.utc),
    )
    parsed = ParsedBuild(
        compiler_errors=[CompileError(file="Main.java", line=42, message="syntax error")],
        error_signature="sha256:compile_sig",
    )
    items = _initial_evidence_items(
        event=event,
        parsed=parsed,
        build_summary={"job": "target-app/main"},
        test_report=None,
        history=[],
    )
    assert any(i.id == "compiler_errors[0]" for i in items)
    assert any(i.id == "error_signature" for i in items)


def test_fallback_evidence_when_hypotheses_empty():
    event = IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="target-app/main",
        build_number=15,
        build_url="http://jenkins:8080/job/target-app/job/main/15/",
        branch="main",
        git_commit="abc1234",
        timestamp=datetime.now(timezone.utc),
    )
    parsed = ParsedBuild()
    evidence = _fallback_evidence(
        event=event,
        parsed=parsed,
        hypotheses=[],
        evidence_items=[],
        tool_calls=[],
    )
    assert evidence.failure_type == FailureTaxonomy.UNKNOWN
    assert len(evidence.root_cause_hypotheses) > 0


@pytest.mark.asyncio
async def test_run_event_handles_jenkins_errors(tmp_path, monkeypatch):
    """When Jenkins client methods raise errors, run_event records failed tool calls
    and completes without crashing."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    class BrokenJenkinsClient:
        async def get_console_text(self, job, num):
            raise RuntimeError("Console unavailable")

        async def get_stage_summary(self, job, num):
            raise RuntimeError("Stage summary unavailable")

        async def get_test_report(self, job, num):
            raise RuntimeError("Test report unavailable")

        async def get_build_history(self, job, limit=30):
            raise RuntimeError("Build history unavailable")

        async def aclose(self):
            pass

    event = IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="target-app/main",
        build_number=15,
        build_url="http://jenkins:8080/job/target-app/job/main/15/",
        branch="main",
        git_commit="abc1234",
        timestamp=datetime.now(timezone.utc),
    )

    announced = []

    async def fake_notifier(msg):
        announced.append(msg)

    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        jenkins_client_factory=lambda: BrokenJenkinsClient(),
        notifier=fake_notifier,
    )
    agent.bb = bb

    evidence = await agent.run_event(event)
    assert evidence.status == "completed"
    assert len(evidence.tool_calls) >= 2
    assert any(not tc.ok for tc in evidence.tool_calls)
    assert len(announced) == 1


def test_run_investigation_missing_fields_validation(tmp_path, monkeypatch):
    """run_investigation raises ValueError when incident context is missing required fields."""
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    incident_id = uuid4()
    # Missing 'branch', 'git_commit', etc.
    bb.post(
        BlackboardEntry(
            incident_id=incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={"job_name": "target-app/main", "build_number": 10},
        )
    )

    with pytest.raises(ValueError, match="missing required fields"):
        run_investigation(incident_id=incident_id)
