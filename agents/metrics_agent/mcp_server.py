"""The Metrics agent's Prometheus tools, exposed as an MCP server.

Three read-only tools over the same `PrometheusTool` the investigator uses:

* `list_catalog`   - the vetted queries the agent investigates with
* `prometheus_query`       - one instant query
* `prometheus_query_range` - a bounded range query

Run: `python -m agents.metrics_agent.mcp_server` (stdio transport, PROMETHEUS_URL from the
environment). Guardrails live here, not in the caller: a time range and a step are bounded,
and results are capped so a careless query cannot flood the caller's context.

Status: the investigator still calls `PrometheusTool` in-process; this server makes the same
capability available to any MCP client but is not yet on the agent's own call path.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .models import QueryResult
from .prometheus_tool import PrometheusError, PrometheusTool
from .queries import CATALOG

MAX_RANGE = timedelta(hours=6)
MAX_SERIES = 20
MAX_SAMPLES_PER_SERIES = 500
_STEP = re.compile(r"^\d+[smh]$")
_STEP_SECONDS = {"s": 1, "m": 60, "h": 3600}


class ToolInputError(ValueError):
    """The request was refused before reaching Prometheus."""


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolInputError(f"invalid ISO-8601 time: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _summarise(result: QueryResult) -> dict[str, Any]:
    series = []
    for item in result.series[:MAX_SERIES]:
        samples = item.samples[-MAX_SAMPLES_PER_SERIES:]
        series.append(
            {
                "labels": item.labels,
                "samples": [[s.timestamp.isoformat(), s.value] for s in samples],
                "truncated": len(samples) < len(item.samples),
            }
        )
    return {
        "promql": result.promql,
        "result_type": result.result_type,
        "series_count": len(result.series),
        "series": series,
        "truncated": len(result.series) > MAX_SERIES,
    }


def build_server(tool: PrometheusTool) -> FastMCP:
    server = FastMCP("jenkins-guardian-metrics")

    @server.tool()
    def list_catalog() -> list[dict[str, str]]:
        """List the vetted Prometheus queries the Metrics agent investigates with."""
        return [{"name": s.name, "promql": s.promql, "unit": s.unit} for s in CATALOG]

    @server.tool()
    def prometheus_query(promql: str, at: str | None = None) -> dict[str, Any]:
        """Run one instant PromQL query, optionally at an ISO-8601 time (default: now)."""
        if not promql.strip():
            raise ToolInputError("promql must not be empty")
        try:
            return _summarise(tool.query(promql, _parse_time(at) if at else None))
        except PrometheusError as exc:
            raise ToolInputError(f"prometheus error: {exc}") from exc

    @server.tool()
    def prometheus_query_range(promql: str, start: str, end: str, step: str = "15s") -> dict[str, Any]:
        """Run a PromQL range query. `start`/`end` are ISO-8601; the range is capped at 6 hours."""
        if not promql.strip():
            raise ToolInputError("promql must not be empty")
        if not _STEP.match(step):
            raise ToolInputError("step must look like 15s, 1m or 1h")
        begin, finish = _parse_time(start), _parse_time(end)
        if finish <= begin:
            raise ToolInputError("end must be after start")
        if finish - begin > MAX_RANGE:
            raise ToolInputError(f"range exceeds the {MAX_RANGE} limit")
        if (finish - begin).total_seconds() / (int(step[:-1]) * _STEP_SECONDS[step[-1]]) > 11_000:
            raise ToolInputError("step too small for this range")
        try:
            return _summarise(tool.query_range(promql, begin, finish, step))
        except PrometheusError as exc:
            raise ToolInputError(f"prometheus error: {exc}") from exc

    return server


def main() -> None:
    build_server(PrometheusTool(os.environ["PROMETHEUS_URL"])).run()


if __name__ == "__main__":
    main()
