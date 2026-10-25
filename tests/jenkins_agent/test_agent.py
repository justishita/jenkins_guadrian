"""Focused tests for the CrewAI Jenkins investigation agent."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

import agents.jenkins_agent.agent as agent_module
from agents.jenkins_agent.agent import JenkinsInvestigationAgent
from agents.jenkins_agent.tools.jenkins_client import ConsoleText
from common.blackboard import FileBlackboard
from common.models import Evidence, IncidentCreatedEvent


EVENT = IncidentCreatedEvent(
    incident_id=uuid4(),
    job_name="orders/main",
    build_number=7,
    build_url="http://jenkins/job/orders/job/main/7/",
    branch="main",
    git_commit="commit-7",
    failed_stage="Tests",
    timestamp=datetime.now(timezone.utc),
)


class FakeJenkinsClient:
    def __init__(self, console: ConsoleText | None = None) -> None:
        self.console = console or ConsoleText(
            "FAILED tests/test_orders.py::test_total - AssertionError: expected 3",
            False,
            False,
        )
        self.closed = False

    async def get_console_text(self, job: str, number: int) -> ConsoleText:
        assert job == EVENT.job_name
        assert number == EVENT.build_number
        return self.console

    async def get_stage_summary(self, job: str, number: int) -> dict[str, object]:
        return {"stages": [{"name": "Tests", "status": "FAILED", "durationMillis": 800}]}

    async def get_test_report(self, job: str, number: int) -> dict[str, int]:
        return {"failCount": 1, "totalCount": 1}

    async def get_build_history(self, job: str) -> list[dict[str, object]]:
        return [{"number": EVENT.build_number, "result": "FAILURE"}]

    async def aclose(self) -> None:
        self.closed = True


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.written: list[Evidence] = []

    async def write(self, evidence: Evidence) -> None:
        self.written.append(evidence)


@pytest.mark.asyncio
async def test_offline_fallback_writes_evidence_to_store_and_blackboard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LLM_OFFLINE", "1")
    blackboard = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: blackboard)
    client = FakeJenkinsClient()
    store = FakeEvidenceStore()
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await agent.run_event(EVENT)

    assert evidence.status == "completed"
    assert evidence.failure_type.value == "code_test_failure"
    assert evidence.redaction_applied
    assert store.written == [evidence]
    assert client.closed
    entry = blackboard.read(EVENT.incident_id, section="evidence", key="jenkins_agent")
    assert entry is not None
    assert entry.payload["failure_type"] == "code_test_failure"


@pytest.mark.asyncio
async def test_console_truncation_is_preserved_in_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LLM_OFFLINE", "1")
    blackboard = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: blackboard)
    store = FakeEvidenceStore()
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: FakeJenkinsClient(
            ConsoleText("partial failure output", True, True)
        ),
    )

    evidence = await agent.run_event(EVENT)

    assert evidence.log_truncated is True
    summary = next(item for item in evidence.evidence_items if item.id == "build_summary")
    assert '"console_truncated": true' in summary.content
    assert '"console_partial": true' in summary.content


@pytest.mark.asyncio
async def test_only_top_five_failing_tests_are_added_to_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LLM_OFFLINE", "1")
    blackboard = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: blackboard)
    log = "\n".join(
        f"FAILED tests/test_many.py::test_case_{index:02d} - AssertionError"
        for index in range(20)
    )
    store = FakeEvidenceStore()
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: FakeJenkinsClient(ConsoleText(log, False, False)),
    )

    evidence = await agent.run_event(EVENT)

    assert len(evidence.failing_tests) == 5
    assert evidence.failing_tests[0] == "test_case_00"
    assert len([item for item in evidence.evidence_items if item.id.startswith("failing_tests[")]) == 5
    summary = next(item for item in evidence.evidence_items if item.id == "build_summary")
    assert '"total_failing_tests": 20' in summary.content
