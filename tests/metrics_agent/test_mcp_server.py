"""The Prometheus MCP server, exercised through a real in-memory MCP client session."""

import json
from datetime import timedelta

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agents.metrics_agent.mcp_server import build_server
from agents.metrics_agent.models import QueryResult
from agents.metrics_agent.queries import CATALOG
from tests.metrics_agent.helpers import FAILURE_TIME, FakeTool

pytestmark = pytest.mark.asyncio

START = "2026-10-05T10:00:00+00:00"
END = "2026-10-05T10:10:00+00:00"


class Tool(FakeTool):
    def query(self, promql, at=None):
        return QueryResult(promql=promql, result_type="vector", series=[])


async def call(name, arguments):
    server = build_server(Tool())._mcp_server
    async with create_connected_server_and_client_session(server) as session:
        return await session.call_tool(name, arguments)


async def test_server_exposes_at_least_two_tools():
    async with create_connected_server_and_client_session(build_server(Tool())._mcp_server) as session:
        names = {t.name for t in (await session.list_tools()).tools}
    assert {"list_catalog", "prometheus_query", "prometheus_query_range"} <= names


async def test_catalog_lists_the_vetted_queries():
    result = await call("list_catalog", {})
    names = {json.loads(block.text)["name"] for block in result.content}  # one block per catalog entry
    assert names == {spec.name for spec in CATALOG}


async def test_range_query_returns_bounded_series():
    promql = CATALOG[1].promql
    start = (FAILURE_TIME - timedelta(minutes=10)).isoformat()
    result = await call("prometheus_query_range", {"promql": promql, "start": start, "end": FAILURE_TIME.isoformat()})
    assert not result.isError
    body = json.loads(result.content[0].text)
    assert body["promql"] == promql and body["series_count"] >= 1
    assert len(body["series"][0]["samples"]) <= 500


@pytest.mark.parametrize(
    "arguments",
    [
        {"promql": "  ", "start": START, "end": END},
        {"promql": "up", "start": END, "end": START},
        {"promql": "up", "start": START, "end": "2026-10-06T10:00:00+00:00"},
        {"promql": "up", "start": START, "end": END, "step": "fast"},
        {"promql": "up", "start": START, "end": "2026-10-05T15:00:00+00:00", "step": "1s"},
        {"promql": "up", "start": "yesterday", "end": END},
    ],
)
async def test_unsafe_range_requests_are_refused_before_reaching_prometheus(arguments):
    tool = Tool()
    server = build_server(tool)._mcp_server
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("prometheus_query_range", arguments)
    assert result.isError
    assert tool.calls == []


async def test_instant_query_rejects_empty_promql():
    assert (await call("prometheus_query", {"promql": ""})).isError
