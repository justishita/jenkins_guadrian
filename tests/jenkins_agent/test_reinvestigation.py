"""Tests for the Jenkins agent reinvestigation flow (attempt > 0)."""

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

import agents.jenkins_agent.agent as agent_module
from agents.jenkins_agent.agent import JenkinsInvestigationAgent
from agents.jenkins_agent.tools.flaky import FlakinessReport
from agents.jenkins_agent.tools.jenkins_client import ConsoleText
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_event(**overrides):
    defaults = dict(
        incident_id=uuid4(),
        job_name="target-app/main",
        build_number=10,
        build_url="http://jenkins:8080/job/target-app/job/main/10/",
        branch="main",
        git_commit="abc123",
        failed_stage="Tests",
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return IncidentCreatedEvent(**defaults)


class FakeEvidenceStore:
    def __init__(self):
        self.written: list[Evidence] = []

    async def write(self, evidence: Evidence) -> None:
        self.written.append(evidence)


class FakeJenkinsClient:
    """Configurable fake Jenkins client for reinvestigation tests."""

    def __init__(
        self,
        *,
        build_result: str = "FAILURE",
        building: bool = False,
        console: str = "FAILED tests/test_orders.py::test_total - AssertionError: expected 3",
        test_report: dict | None = None,
        console_truncated: bool = False,
        console_partial: bool = False,
    ):
        self._build_result = build_result
        self._building = building
        self._console = console
        self._test_report = test_report or {"failCount": 1, "totalCount": 50}
        self._console_truncated = console_truncated
        self._console_partial = console_partial
        self.closed = False

    async def _wait_for_build_completion(self, job, number):
        return {
            "number": number,
            "result": self._build_result,
            "building": self._building,
        }, self._building

    async def get_build(self, job, number):
        return {"number": number, "result": self._build_result, "building": self._building}

    async def get_console_text(self, job, number):
        return ConsoleText(self._console, self._console_truncated, self._console_partial)

    async def get_stage_summary(self, job, number):
        return {"stages": [{"name": "Tests", "status": "FAILED", "durationMillis": 800}]}

    async def get_test_report(self, job, number):
        return self._test_report

    async def get_build_history(self, job, limit=30):
        return [{"number": 10, "result": "FAILURE"}]

    async def aclose(self):
        self.closed = True


def _seed_original_evidence(bb, incident_id, *, error_signature="sha256:original_sig", test_total=50):
    """Post original attempt-0 evidence to the blackboard."""
    payload = {
        "incident_id": str(incident_id),
        "agent": "jenkins_agent",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "failure_type": "code_test_failure",
        "summary": "Original failure: test assertion error",
        "root_cause_hypotheses": [
            {
                "hypothesis": "Test test_total failed due to assertion error",
                "failure_type": "code_test_failure",
                "confidence": 0.7,
                "supporting_evidence": ["build_summary"],
                "contradicting_evidence": [],
            }
        ],
        "evidence_items": [
            {
                "id": "build_summary",
                "kind": "log_excerpt",
                "source": "jenkins:target-app/main#10",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": json.dumps({"job_name": "target-app/main", "build_number": 10}),
            },
            {
                "id": "test_report",
                "kind": "test_result",
                "source": "jenkins:target-app/main#10",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": json.dumps({"totalCount": test_total, "failCount": 1}),
            },
        ],
        "tool_calls": [],
        "confidence": 0.7,
        "recommended_next_steps": ["Fix test_total"],
        "error_signature": error_signature,
        "redaction_applied": True,
    }
    bb.post(
        BlackboardEntry(
            incident_id=incident_id,
            section="evidence",
            key="jenkins_agent",
            author="jenkins_agent",
            payload=payload,
        )
    )


# ---------------------------------------------------------------------------
# Test 1: Same error signature → fix didn't address root cause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinvestigation_same_signature(monkeypatch, tmp_path):
    """When the new build's error signature matches the original, the agent
    reports that the fix did not address the root cause."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()
    _seed_original_evidence(bb, event.incident_id, error_signature="sha256:original_sig")

    # Seed the context entry required by run_investigation
    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    # The fake client returns console text that parses to the SAME error_signature
    # We patch compute_error_signature to return the same sig
    original_parse_build = agent_module.parse_build

    def patched_parse_build(text, **kwargs):
        result = original_parse_build(text, **kwargs)
        # Force same signature
        result.error_signature = "sha256:original_sig"
        return result

    monkeypatch.setattr(agent_module, "parse_build", patched_parse_build)

    store = FakeEvidenceStore()
    client = FakeJenkinsClient(build_result="FAILURE")
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await agent.run_reinvestigation(
        event,
        attempt=2,
        previous_failure_summary="Validation attempt 1 failed with FAILURE",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert evidence.status == "completed"
    assert "same_signature" in evidence.summary.lower() or "didn't address" in evidence.summary.lower() or "did not address" in evidence.summary.lower()

    # Verify previous_attempts evidence item exists
    prev_item = next((i for i in evidence.evidence_items if i.id == "previous_attempts"), None)
    assert prev_item is not None
    content = json.loads(prev_item.content)
    assert content["signature_comparison"] == "same_signature"
    assert content["attempt"] == 2
    assert len(content["previous_hypotheses"]) > 0
    assert client.closed


# ---------------------------------------------------------------------------
# Test 2: New/different error signature → fix introduced a new problem
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinvestigation_new_signature(monkeypatch, tmp_path):
    """When the new build's error signature differs, the agent reports the fix
    introduced or exposed a new problem."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()
    _seed_original_evidence(bb, event.incident_id, error_signature="sha256:original_sig")

    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    original_parse_build = agent_module.parse_build

    def patched_parse_build(text, **kwargs):
        result = original_parse_build(text, **kwargs)
        # Force DIFFERENT signature
        result.error_signature = "sha256:completely_new_sig"
        return result

    monkeypatch.setattr(agent_module, "parse_build", patched_parse_build)

    store = FakeEvidenceStore()
    client = FakeJenkinsClient(
        build_result="FAILURE",
        console="ERROR: NullPointerException at Main.java:42",
    )
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await agent.run_reinvestigation(
        event,
        attempt=2,
        previous_failure_summary="Validation attempt 1 failed with FAILURE",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert evidence.status == "completed"
    assert "new_signature" in evidence.summary.lower() or "introduced" in evidence.summary.lower() or "exposed" in evidence.summary.lower() or "differs" in evidence.summary.lower()

    prev_item = next((i for i in evidence.evidence_items if i.id == "previous_attempts"), None)
    assert prev_item is not None
    content = json.loads(prev_item.content)
    assert content["signature_comparison"] == "new_signature"
    assert client.closed


# ---------------------------------------------------------------------------
# Test 3: New build passed → record success, check test counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinvestigation_build_passed(monkeypatch, tmp_path):
    """When the validation build passed, the agent records success and exits
    without running CrewAI / LLM analysis."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()
    _seed_original_evidence(bb, event.incident_id, test_total=50)

    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    store = FakeEvidenceStore()
    client = FakeJenkinsClient(
        build_result="SUCCESS",
        test_report={"totalCount": 50, "failCount": 0},
    )
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await agent.run_reinvestigation(
        event,
        attempt=2,
        previous_failure_summary="Validation attempt 1 failed",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert evidence.status == "completed"
    assert evidence.confidence == 1.0
    assert "passed" in evidence.summary.lower()
    assert any("close incident" in step.lower() for step in evidence.recommended_next_steps)
    # No test count warning when counts are equal
    assert "WARNING" not in evidence.summary
    assert client.closed


@pytest.mark.asyncio
async def test_reinvestigation_build_passed_with_test_count_drop(monkeypatch, tmp_path):
    """When the validation build passed but test count dropped, the agent
    flags a warning about possible test deletion."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()
    _seed_original_evidence(bb, event.incident_id, test_total=50)

    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    store = FakeEvidenceStore()
    # Test count dropped from 50 to 30
    client = FakeJenkinsClient(
        build_result="SUCCESS",
        test_report={"totalCount": 30, "failCount": 0},
    )
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await agent.run_reinvestigation(
        event,
        attempt=2,
        previous_failure_summary="Validation attempt 1 failed",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert evidence.status == "completed"
    # Confidence should be reduced when test count drops
    assert evidence.confidence < 1.0
    assert "dropped" in evidence.summary.lower() or "WARNING" in evidence.summary
    assert any("test deletion" in step.lower() or "opa" in step.lower() for step in evidence.recommended_next_steps)
    assert client.closed


# ---------------------------------------------------------------------------
# Test 4: Build still running → defer gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinvestigation_build_still_running(monkeypatch, tmp_path):
    """When the validation build is still running, the agent records
    insufficient_evidence and exits without crashing."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()
    _seed_original_evidence(bb, event.incident_id)

    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    store = FakeEvidenceStore()
    client = FakeJenkinsClient(building=True)
    agent = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await agent.run_reinvestigation(
        event,
        attempt=2,
        previous_failure_summary="Validation attempt 1 failed",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert evidence.status == "insufficient_evidence"
    assert evidence.confidence == 0.0
    assert "still running" in evidence.summary.lower()
    assert any("wait" in step.lower() for step in evidence.recommended_next_steps)
    assert evidence.failure_type == FailureTaxonomy.UNKNOWN
    # Should still post to blackboard and evidence store
    assert len(store.written) == 1
    bb_entry = bb.read(event.incident_id, section="evidence", key="jenkins_agent")
    assert bb_entry is not None
    assert client.closed


# ---------------------------------------------------------------------------
# Test 5: run_investigation routes to reinvestigation for attempt > 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_investigation_routes_reinvestigation(monkeypatch, tmp_path):
    """Verify that run_investigation dispatches to run_reinvestigation when
    attempt > 0 and new_build_url is provided."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()

    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )
    _seed_original_evidence(bb, event.incident_id)

    # Track which method was called
    calls = {"run_event": 0, "run_reinvestigation": 0}

    original_run_event = JenkinsInvestigationAgent.run_event
    original_run_reinvestigation = None  # Will be set after patching

    async def tracked_run_reinvestigation(self, ev, *, attempt, previous_failure_summary, new_build_url):
        calls["run_reinvestigation"] += 1
        # Return minimal valid evidence
        return Evidence(
            incident_id=ev.incident_id,
            agent="jenkins_agent",
            created_at=datetime.now(timezone.utc),
            status="completed",
            failure_type=FailureTaxonomy.UNKNOWN,
            summary="Test reinvestigation",
            evidence_items=[],
            tool_calls=[],
            confidence=0.5,
            redaction_applied=True,
        )

    monkeypatch.setattr(JenkinsInvestigationAgent, "run_reinvestigation", tracked_run_reinvestigation)

    from agents.jenkins_agent.agent import run_investigation

    evidence = run_investigation(
        incident_id=event.incident_id,
        attempt=2,
        previous_failure_summary="Previous failure summary",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert calls["run_reinvestigation"] == 1
    assert evidence.status == "completed"


# ---------------------------------------------------------------------------
# Test 6: _parse_build_url helper
# ---------------------------------------------------------------------------


def test_parse_build_url_standard():
    from agents.jenkins_agent.agent import _parse_build_url

    event = _make_event()
    job, num = _parse_build_url(
        "http://jenkins:8080/job/target-app/job/main/42/", event
    )
    assert job == "target-app/main"
    assert num == 42


def test_parse_build_url_change_request():
    from agents.jenkins_agent.agent import _parse_build_url

    event = _make_event()
    job, num = _parse_build_url(
        "http://jenkins:8080/job/target-app/view/change-requests/job/PR-7/5/", event
    )
    assert "target-app" in job
    assert "PR-7" in job
    assert num == 5


def test_parse_build_url_fallback():
    from agents.jenkins_agent.agent import _parse_build_url

    event = _make_event()
    job, num = _parse_build_url("not-a-valid-url", event)
    assert job == event.job_name
    assert num == event.build_number


# ---------------------------------------------------------------------------
# Test A: MAX_ATTEMPTS is enforced by run_investigation
# ---------------------------------------------------------------------------


def test_max_attempts_enforced(monkeypatch, tmp_path):
    """run_investigation raises ValueError when attempt >= MAX_ATTEMPTS."""
    from agents.jenkins_agent.agent import MAX_ATTEMPTS, run_investigation

    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    incident_id = uuid4()

    # Seed context entry so it could be found (we expect to raise before reading)
    bb.post(
        BlackboardEntry(
            incident_id=incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": "target-app/main",
                "build_number": 10,
                "build_url": "http://jenkins:8080/job/target-app/job/main/10/",
                "branch": "main",
                "git_commit": "abc123",
                "failed_stage": "Tests",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )

    with pytest.raises(ValueError, match="MAX_ATTEMPTS"):
        run_investigation(
            incident_id=incident_id,
            attempt=MAX_ATTEMPTS,
            previous_failure_summary="some summary",
            new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
        )


# ---------------------------------------------------------------------------
# Test B: Flakiness re-check on the new build
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flaky_recheck_on_new_build(monkeypatch, tmp_path):
    """When the new build fails with a flaky test, flakiness re-check fires and
    the resulting evidence carries flaky_score >= 0.6 and failure_type FLAKY_TEST."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()
    _seed_original_evidence(bb, event.incident_id, error_signature="sha256:original_sig")
    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    # Build a FlakinessReport with high flaky_score
    from agents.jenkins_agent.tools.flaky import BuildTestRecord

    fake_flaky_report = FlakinessReport(
        test_name="test_flaky",
        job=event.job_name,
        pass_count=5,
        fail_count=5,
        total_runs=10,
        transitions=8,
        same_commit_both_passed_and_failed=True,
        regression_detected=False,
        regression_commit=None,
        flaky_score=0.8,
        is_flaky=True,
        status="flaky",
        confidence=0.9,
        history=[
            BuildTestRecord(build_number=i, passed=(i % 2 == 0)) for i in range(1, 11)
        ],
        details="Test oscillates across builds on the same commit.",
    )

    async def fake_analyze_test_flakiness(job, test_name, client):
        return fake_flaky_report

    monkeypatch.setattr(agent_module, "analyze_test_flakiness", fake_analyze_test_flakiness)

    # Console text that contains a flaky test failure so parse_build picks it up
    flaky_console = (
        "FAILED tests/test_orders.py::test_flaky - AssertionError: intermittent failure\n"
        "ERROR: test_flaky failed"
    )
    store = FakeEvidenceStore()
    client = FakeJenkinsClient(build_result="FAILURE", console=flaky_console)

    investigator = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await investigator.run_reinvestigation(
        event,
        attempt=2,
        previous_failure_summary="Original failure: test_flaky",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert evidence.flaky_score is not None
    assert evidence.flaky_score >= 0.6
    assert any(item.id == "flakiness" for item in evidence.evidence_items)
    assert evidence.failure_type == FailureTaxonomy.FLAKY_TEST


# ---------------------------------------------------------------------------
# Test C: trigger_build_url posts to the given URL; 200/201 succeed, 404/401 raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_build_explicit_url(monkeypatch, tmp_path):
    """JenkinsClient.trigger_build_url() posts to the given URL.
    200 and 201 are both accepted as success; 404 raises JenkinsNotFound;
    401 raises JenkinsAuthError."""
    import httpx
    import respx

    from agents.jenkins_agent.tools.jenkins_client import (
        JenkinsAuthError,
        JenkinsClient,
        JenkinsNotFound,
    )

    monkeypatch.setenv("JENKINS_URL", "http://jenkins:8080")
    monkeypatch.setenv("JENKINS_USER", "admin")
    monkeypatch.setenv("JENKINS_API_TOKEN", "test-token")

    trigger_url = "http://jenkins:8080/job/target-app/view/change-requests/job/PR-7/build"

    # --- 201 Created → success ---
    with respx.mock:
        respx.post(trigger_url).mock(return_value=httpx.Response(201))
        async with JenkinsClient(max_retries=0) as client:
            # Should NOT raise
            await client.trigger_build_url(trigger_url)

    # --- 200 OK → success ---
    with respx.mock:
        respx.post(trigger_url).mock(return_value=httpx.Response(200))
        async with JenkinsClient(max_retries=0) as client:
            await client.trigger_build_url(trigger_url)

    # --- 404 → JenkinsNotFound ---
    with respx.mock:
        respx.post(trigger_url).mock(return_value=httpx.Response(404))
        with pytest.raises(JenkinsNotFound):
            async with JenkinsClient(max_retries=0) as client:
                await client.trigger_build_url(trigger_url)

    # --- 401 → JenkinsAuthError ---
    with respx.mock:
        respx.post(trigger_url).mock(return_value=httpx.Response(401))
        with pytest.raises(JenkinsAuthError):
            async with JenkinsClient(max_retries=0) as client:
                await client.trigger_build_url(trigger_url)


# ---------------------------------------------------------------------------
# Test D: Same failed_stage + high flakiness → FLAKY_TEST + flaky recommendation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinvestigation_flaky_same_stage(monkeypatch, tmp_path):
    """When the new build fails at the SAME stage as the original but flakiness
    score is high (>0.4), failure_type should be FLAKY_TEST and recommended_next_steps
    should include a flaky recommendation."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event(failed_stage="Tests")
    _seed_original_evidence(bb, event.incident_id, error_signature="sha256:original_sig")
    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    from agents.jenkins_agent.tools.flaky import BuildTestRecord

    fake_flaky_report = FlakinessReport(
        test_name="test_flaky_stage",
        job=event.job_name,
        pass_count=3,
        fail_count=7,
        total_runs=10,
        transitions=6,
        same_commit_both_passed_and_failed=True,
        regression_detected=False,
        regression_commit=None,
        flaky_score=0.75,
        is_flaky=True,
        status="flaky",
        confidence=0.85,
        history=[
            BuildTestRecord(build_number=i, passed=(i % 3 == 0)) for i in range(1, 11)
        ],
        details="Oscillating test in the Tests stage.",
    )

    async def fake_analyze_flakiness_same_stage(job, test_name, client):
        return fake_flaky_report

    monkeypatch.setattr(
        agent_module, "analyze_test_flakiness", fake_analyze_flakiness_same_stage
    )

    # Console text with a test failure so parse_build produces a failing_test
    console = (
        "FAILED tests/test_orders.py::test_flaky_stage - AssertionError: intermittent\n"
    )
    store = FakeEvidenceStore()
    client = FakeJenkinsClient(build_result="FAILURE", console=console)

    investigator = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    evidence = await investigator.run_reinvestigation(
        event,
        attempt=1,
        previous_failure_summary="Build failed in Tests stage",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    assert evidence.failure_type == FailureTaxonomy.FLAKY_TEST
    # Flaky recommendation should appear in recommended_next_steps
    assert any(
        "retry" in step.lower() or "quarantine" in step.lower() or "flak" in step.lower()
        for step in evidence.recommended_next_steps
    )


# ---------------------------------------------------------------------------
# Test E: Blackboard version increments with each reinvestigation write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinvestigation_blackboard_version_increments(monkeypatch, tmp_path):
    """After run_reinvestigation writes to the Blackboard, the new entry's version
    should be greater than the original attempt-0 evidence version (= 1)."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)

    event = _make_event()
    # Seed original evidence – this creates version 1 in the blackboard
    _seed_original_evidence(bb, event.incident_id, error_signature="sha256:original_sig")
    bb.post(
        BlackboardEntry(
            incident_id=event.incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": event.job_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "branch": event.branch,
                "git_commit": event.git_commit,
                "failed_stage": event.failed_stage,
                "timestamp": event.timestamp.isoformat(),
            },
        )
    )

    # Verify version 1 is in place
    v1_entry = bb.read(event.incident_id, section="evidence", key="jenkins_agent")
    assert v1_entry is not None
    assert v1_entry.version == 1

    store = FakeEvidenceStore()
    client = FakeJenkinsClient(build_result="FAILURE")

    investigator = JenkinsInvestigationAgent(
        llm_client=object(),
        evidence_store=store,
        jenkins_client_factory=lambda: client,
    )

    await investigator.run_reinvestigation(
        event,
        attempt=2,
        previous_failure_summary="Attempt 1 failed",
        new_build_url="http://jenkins:8080/job/target-app/job/main/11/",
    )

    # The latest entry on the blackboard should now have version > 1
    latest_entry = bb.read(event.incident_id, section="evidence", key="jenkins_agent")
    assert latest_entry is not None
    assert latest_entry.version == 3  # attempt + 1 where attempt = 2
