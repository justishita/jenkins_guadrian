"""Tests for MCP servers and decoupling."""

import inspect
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import respx
import httpx

from mcp_servers.jenkins_mcp import server as j_server
from mcp_servers.analytics_mcp import server as a_server
from common.blackboard import BlackboardEntry, FileBlackboard
from common.mcp_client import get_jenkins_mcp_tools, get_analytics_mcp_tools


@pytest.mark.asyncio
async def test_analytics_mcp_tools():
    # Test compute_signature
    result = a_server.compute_signature(
        failed_stage="test",
        error_blocks=["FAILED test_app.py", "AssertionError: expected 1 got 2"],
    )
    assert "error_signature" in result
    assert len(result["error_signature"]) == 64

    flaky = await a_server.check_flakiness("test_order_flow")
    assert flaky["test_name"] == "test_order_flow"
    assert flaky["is_flaky"] is False


@pytest.mark.asyncio
@respx.mock
async def test_jenkins_mcp_tools_with_respx(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "http://jenkins:8080")
    monkeypatch.setenv("JENKINS_USER", "test-user")
    monkeypatch.setenv("JENKINS_API_TOKEN", "test-token")
    j_server._JENKINS_CLIENT = None
    respx.get("http://jenkins:8080/job/test-job/1/api/json").mock(
        return_value=httpx.Response(200, json={"number": 1, "result": "FAILURE", "building": False})
    )
    respx.get("http://jenkins:8080/job/test-job/1/consoleText").mock(
        return_value=httpx.Response(200, text="Build failed\nError in line 10\nFinished: FAILURE")
    )

    summary = await j_server.get_build_summary("test-job", 1)
    assert summary["number"] == 1
    assert summary["result"] == "FAILURE"

    blocks = await j_server.get_error_blocks("test-job", 1)
    assert "error_blocks" in blocks
    await j_server._JENKINS_CLIENT.aclose()
    j_server._JENKINS_CLIENT = None


def test_agent_package_does_not_call_external_clients_directly():
    from common.mcp_client import get_jenkins_mcp_tools
    tools = get_jenkins_mcp_tools("test-job", 1)
    assert len(tools) >= 7
    tool_names = [t.name for t in tools]
    assert "get_build_summary" in tool_names
    assert "get_stage_summary" in tool_names


# --- find_related_incidents: reads earlier incidents from the Blackboard -------------------


def _post_incident(bb: FileBlackboard, signature: str, *, status: str = "completed", branch: str = "main",
                   age: timedelta = timedelta(hours=1), job: str = "target-app/main") -> str:
    incident_id = uuid4()
    when = (datetime.now(timezone.utc) - age).isoformat()
    bb.post(BlackboardEntry(incident_id=incident_id, section="context", key="incident_event", author="backend",
                            payload={"job_name": job, "branch": branch, "timestamp": when}))
    bb.post(BlackboardEntry(incident_id=incident_id, section="evidence", key="jenkins_agent", author="jenkins_agent",
                            payload={"status": status, "failure_type": "code_test_failure",
                                     "error_signature": signature, "confidence": 0.9, "created_at": when}))
    return str(incident_id)


@pytest.fixture
def board(tmp_path, monkeypatch):
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(a_server, "_blackboard", lambda: bb)
    return bb


async def test_find_related_incidents_returns_matches_newest_first(board):
    older = _post_incident(board, "sig-a", age=timedelta(days=3))
    newer = _post_incident(board, "sig-a", age=timedelta(hours=2))
    _post_incident(board, "sig-other")

    result = await a_server.find_related_incidents("sig-a")

    assert result["related_incident_ids"] == [newer, older]
    assert result["similarity_scores"] == [1.0, 1.0]
    assert result["recurrence_count"] == 2
    assert "2 earlier incident(s)" in result["message"]
    assert "error_signature" not in result["related_incidents"][0]
    assert result["related_incidents"][0]["incident_id"] == newer


async def test_find_related_incidents_without_a_match_says_so(board):
    _post_incident(board, "sig-a")

    result = await a_server.find_related_incidents("sig-none")

    assert result["related_incident_ids"] == []
    assert result["recurrence_count"] == 0
    assert "No earlier incident" in result["message"]


async def test_find_related_incidents_requires_a_signature(board):
    result = await a_server.find_related_incidents("   ")

    assert result["related_incident_ids"] == []
    assert "required" in result["message"]


async def test_find_related_incidents_respects_the_window_and_excludes_the_current_incident(board):
    _post_incident(board, "sig-a", age=timedelta(days=10))
    current = _post_incident(board, "sig-a", age=timedelta(hours=1))

    assert (await a_server.find_related_incidents("sig-a", window_days=7))["related_incident_ids"] == [current]
    excluded = await a_server.find_related_incidents("sig-a", window_days=30, current_incident_id=current)
    assert current not in excluded["related_incident_ids"]
    assert excluded["recurrence_count"] == 1


async def test_find_related_incidents_flags_cross_branch_and_fixed_regressions(board):
    _post_incident(board, "sig-a", status="RESOLVED", branch="release")

    result = await a_server.find_related_incidents("sig-a", job_name="target-app/main")

    assert result["cross_branch"] is True
    assert result["regression_of_fixed_incident"] is True


async def test_find_related_incidents_answers_when_the_blackboard_is_unreadable(monkeypatch):
    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(a_server, "_blackboard", broken)

    result = await a_server.find_related_incidents("sig-a")

    assert result["related_incident_ids"] == []
    assert "unavailable" in result["message"]


# --- check_flakiness -----------------------------------------------------------------


class _FakeReport:
    is_flaky = True
    flaky_score = 0.62
    total_runs = 14
    details = "7 transitions across 14 builds"


class _FakeJenkins:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_check_flakiness_reports_the_jenkins_history(monkeypatch):
    from agents.jenkins_agent.tools import flaky, jenkins_client

    seen = {}

    async def analyze(job, test_name, client):
        seen["args"] = (job, test_name)
        return _FakeReport()

    monkeypatch.setattr(flaky, "analyze_test_flakiness", analyze)
    monkeypatch.setattr(jenkins_client, "JenkinsClient", _FakeJenkins)

    result = await a_server.check_flakiness("test_order_flow", job="target-app/main")

    assert seen["args"] == ("target-app/main", "test_order_flow")
    assert result["is_flaky"] is True
    assert result["flakiness_score"] == 0.62
    assert result["history_runs"] == 14
    assert result["message"] == "7 transitions across 14 builds"


@pytest.mark.asyncio
async def test_check_flakiness_says_why_when_jenkins_is_not_available(monkeypatch):
    for name in ("JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    result = await a_server.check_flakiness("test_order_flow")

    assert result["is_flaky"] is False and result["history_runs"] == 0
    assert "unavailable" in result["message"]
    assert "stub" not in result["message"].lower()


def test_analytics_image_ships_what_check_flakiness_needs():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "mcp_servers/analytics_mcp/Dockerfile").read_text()
    for needed in ("tools/flaky.py", "tools/jenkins_client.py"):
        assert needed in dockerfile
    assert "httpx" in (root / "mcp_servers/analytics_mcp/requirements.txt").read_text()
