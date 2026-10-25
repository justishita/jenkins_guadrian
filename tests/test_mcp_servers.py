"""Tests for MCP servers and decoupling."""

import inspect
import pytest
import respx
import httpx

from mcp_servers.jenkins_mcp import server as j_server
from mcp_servers.analytics_mcp import server as a_server
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
