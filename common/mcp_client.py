"""CrewAI adapters for MCP tools exposed by remote streamable-HTTP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from crewai.tools import BaseTool
from pydantic import Field

from common.guardrails import screen_untrusted_text
from common.redaction import redact, redact_value

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 2


def _screen_tool_value(value: Any) -> Any:
    """Redact and neutralize untrusted text returned by MCP tools before LLM use."""
    if isinstance(value, str):
        return screen_untrusted_text(redact(value), source="mcp_tool_result").text
    if isinstance(value, dict):
        return {str(key): _screen_tool_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_screen_tool_value(item) for item in value]
    return value


class MCPToolWrapper(BaseTool):
    """Expose one remote MCP tool as a bounded, redacting CrewAI tool."""

    name: str
    description: str
    func: Any = Field(default=None, exclude=True)
    max_usage_count: int = 3
    current_usage_count: int = 0
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def _run(self, **kwargs: Any) -> str:
        if self.current_usage_count >= self.max_usage_count:
            return json.dumps(
                {
                    "error": "MCP tool usage cap reached",
                    "error_type": "UsageCapExceeded",
                }
            )

        self.current_usage_count += 1
        try:
            coroutine = self.func(**kwargs)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(asyncio.run, coroutine).result(
                        timeout=self.timeout_seconds * (MAX_RETRIES + 1) + 2
                    )
            else:
                result = asyncio.run(coroutine)

            if isinstance(result, (dict, list)):
                return json.dumps(redact_value(result), default=str)
            return redact(str(result))
        except Exception as error:
            logger.warning("MCP tool %s failed (%s)", self.name, type(error).__name__)
            return json.dumps(
                {"error": redact(str(error)), "error_type": type(error).__name__}
            )


async def _call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
) -> Any:
    """Call a tool over the official MCP streamable-HTTP client."""
    try:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as error:
        raise RuntimeError("The MCP SDK is required to invoke remote tools") from error

    async def invoke() -> Any:
        async with streamablehttp_client(server_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                if result.isError:
                    raise RuntimeError(f"MCP tool {tool_name} reported an error")
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    return _screen_tool_value(redact_value(structured))
                text = "\n".join(
                    block.text
                    for block in result.content
                    if getattr(block, "text", None)
                )
                try:
                    return _screen_tool_value(redact_value(json.loads(text)))
                except json.JSONDecodeError:
                    return _screen_tool_value(redact(text))

    transient_errors = (httpx.TimeoutException, httpx.TransportError, TimeoutError, OSError)
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(invoke(), timeout=timeout_seconds)
        except transient_errors:
            if attempt == max_retries:
                raise
            await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
    raise RuntimeError("MCP tool invocation failed")


def _remote_tool(
    server_variable: str,
    tool_name: str,
    default_url: str,
    defaults: dict[str, Any] | None = None,
) -> Any:
    server_url = os.getenv(server_variable, default_url)

    async def invoke(**arguments: Any) -> Any:
        merged_arguments = {**(defaults or {}), **arguments}
        return await _call_mcp_tool(server_url, tool_name, merged_arguments)

    return invoke


def get_jenkins_mcp_tools(job_name: str = "", build_number: int = 0) -> list[BaseTool]:
    """Return remote read-only Jenkins MCP tools bound to the current build."""
    server_url = os.getenv("MCP_JENKINS_URL", "http://mcp-jenkins:8081/mcp")
    build_defaults = {"job_name": job_name, "build_number": build_number}
    tools = [
        ("get_build_summary", "Return current Jenkins build summary."),
        ("get_stage_summary", "Return pipeline stage results and durations."),
        ("get_error_blocks", "Return Jenkins console error blocks."),
        ("get_test_report", "Return the Jenkins test report, when available."),
        ("compare_with_last_success", "Compare this build with the latest successful build."),
    ]
    result = [
        MCPToolWrapper(
            name=name,
            description=description,
            func=_remote_tool("MCP_JENKINS_URL", name, server_url, build_defaults),
        )
        for name, description in tools
    ]
    result.extend(
        [
            MCPToolWrapper(
                name="get_log_range",
                description="Return an inclusive console line range capped at 300 lines.",
                func=_remote_tool(
                    "MCP_JENKINS_URL",
                    "get_log_range",
                    server_url,
                    {**build_defaults, "start_line": 1, "end_line": 100},
                ),
            ),
            MCPToolWrapper(
                name="get_build_history",
                description="Return up to 30 recent Jenkins builds.",
                func=_remote_tool(
                    "MCP_JENKINS_URL",
                    "get_build_history",
                    server_url,
                    {"job_name": job_name},
                ),
            ),
        ]
    )
    return result


def get_analytics_mcp_tools() -> list[BaseTool]:
    """Return remote analytics MCP tools."""
    server_url = os.getenv("MCP_ANALYTICS_URL", "http://mcp-analytics:8082/mcp")
    tools = [
        (
            "compute_error_signature",
            "Compute a deterministic signature from a stage and error blocks.",
            "compute_signature",
        ),
        ("check_flakiness", "Check whether a test is known to be flaky.", "check_flakiness"),
        (
            "find_related_incidents",
            "Find incidents sharing an error signature.",
            "find_related_incidents",
        ),
    ]
    return [
        MCPToolWrapper(
            name=crew_name,
            description=description,
            func=_remote_tool("MCP_ANALYTICS_URL", mcp_name, server_url),
        )
        for crew_name, description, mcp_name in tools
    ]
