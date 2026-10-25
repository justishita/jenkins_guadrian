"""Jenkins MCP Server (Owner: Person 1).

Exposes read-only Jenkins inspection tools via the official MCP SDK (FastMCP).
Wraps JenkinsClient and parsing helpers. Token only read from env.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from agents.jenkins_agent.parsing import parse_build
from agents.jenkins_agent.parsing.normalization import (
    BuildStage,
    CompileError,
    FailingTest,
    ParsedBuild,
    extract_error_blocks,
    strip_ansi,
)
from agents.jenkins_agent.tools.jenkins_client import JenkinsClient, JenkinsError
from common.redaction import redact, redact_value

logger = logging.getLogger(__name__)

mcp = FastMCP("jenkins_mcp")
mcp.settings.host = "0.0.0.0"
mcp.settings.port = 8081
_JENKINS_CLIENT: JenkinsClient | None = None


# --- Return Models ---


class ToolError(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error: str
    error_type: str


class BuildSummaryResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str = "build_summary"
    number: int | None = None
    result: str | None = None
    building: bool = False
    duration: int | None = None
    timestamp: int | None = None


class StageInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str
    name: str
    result: str | None = None
    duration_ms: float | None = None


class StageSummaryResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    stages: list[StageInfo] = Field(default_factory=list)


class ErrorBlockInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str
    content: str


class ErrorBlocksResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    failing_test_summary: dict[str, Any] = Field(default_factory=dict)
    error_blocks: list[ErrorBlockInfo] = Field(default_factory=list)


class LogRangeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start_line: int
    end_line: int
    lines: list[str] = Field(default_factory=list)


class TestReportResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str | None = None
    report: dict[str, Any] | None = None


class BuildHistoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int
    result: str | None = None
    timestamp: int | None = None
    duration: int | None = None


class BuildHistoryResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str | None = "build_history"
    builds: list[BuildHistoryItem] = Field(default_factory=list)


class CompareSuccessResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str | None = None
    incident_commit: str | None = None
    last_successful_build: dict[str, Any] | None = None
    last_success_commit: str | None = None
    same_commit: bool | None = None
    tests_passed: bool | None = None


def _get_client() -> JenkinsClient:
    global _JENKINS_CLIENT
    if _JENKINS_CLIENT is None:
        _JENKINS_CLIENT = JenkinsClient(
            base_url=os.getenv("JENKINS_URL", "http://jenkins:8080"),
            username=os.getenv("JENKINS_USER") or os.getenv("JENKINS_ADMIN_USER"),
            api_token=os.getenv("JENKINS_API_TOKEN") or os.getenv("JENKINS_ADMIN_PASSWORD"),
        )
    return _JENKINS_CLIENT


@mcp.tool()
async def get_build_summary(job_name: str, build_number: int) -> dict[str, Any]:
    """Return the current Jenkins build result, number, duration, and status."""
    try:
        client = _get_client()
        info = await client.get_build(job_name, build_number)
        return BuildSummaryResult(
            number=info.get("number"),
            result=info.get("result"),
            building=info.get("building", False),
            duration=info.get("duration"),
            timestamp=info.get("timestamp"),
        ).model_dump(mode="json")
    except Exception as e:
        logger.warning("get_build_summary failed (%s)", type(e).__name__)
        return ToolError(error=redact(str(e)), error_type=type(e).__name__).model_dump(mode="json")


@mcp.tool()
async def get_stage_summary(job_name: str, build_number: int) -> dict[str, Any]:
    """Return the normalized Jenkins pipeline stage names and results."""
    try:
        client = _get_client()
        stages_data = await client.get_stage_summary(job_name, build_number)
        stages = []
        raw_stages = stages_data.get("stages", []) if isinstance(stages_data, dict) else []
        for index, s in enumerate(raw_stages):
            name = redact(str(s.get("name", "unknown")))
            stages.append(
                StageInfo(
                    evidence_id=f"stages[{index}]",
                    name=name,
                    result=s.get("status") or s.get("result"),
                    duration_ms=s.get("durationMillis"),
                )
            )
        return StageSummaryResult(stages=stages).model_dump(mode="json")
    except Exception as e:
        logger.warning("get_stage_summary failed (%s)", type(e).__name__)
        return ToolError(error=redact(str(e)), error_type=type(e).__name__).model_dump(mode="json")


@mcp.tool()
async def get_error_blocks(job_name: str, build_number: int) -> dict[str, Any]:
    """Return extracted console error blocks with their stable evidence IDs."""
    try:
        client = _get_client()
        console = await client.get_console_text(job_name, build_number)
        cleaned = strip_ansi(console.text)
        blocks = extract_error_blocks(cleaned)
        error_items = [
            ErrorBlockInfo(evidence_id=f"error_blocks[{i}]", content=redact(b))
            for i, b in enumerate(blocks)
        ]
        return ErrorBlocksResult(
            failing_test_summary={},
            error_blocks=error_items,
        ).model_dump(mode="json")
    except Exception as e:
        logger.warning("get_error_blocks failed (%s)", type(e).__name__)
        return ToolError(error=redact(str(e)), error_type=type(e).__name__).model_dump(mode="json")


@mcp.tool()
async def get_log_range(job_name: str, build_number: int, start_line: int, end_line: int) -> dict[str, Any]:
    """Return an inclusive console line range, capped at 300 lines per call."""
    try:
        if end_line < start_line:
            raise ValueError(f"end_line {end_line} must be >= start_line {start_line}")
        if (end_line - start_line + 1) > 300:
            raise ValueError("Requested line range exceeds 300 lines cap")

        client = _get_client()
        console = await client.get_console_text(job_name, build_number)
        lines = console.text.splitlines()
        excerpt = lines[max(0, start_line - 1): min(len(lines), end_line)]
        safe_lines = [redact(line) for line in excerpt]

        return LogRangeResult(
            start_line=start_line,
            end_line=min(end_line, len(lines)),
            lines=safe_lines,
        ).model_dump(mode="json")
    except Exception as e:
        logger.warning("get_log_range failed (%s)", type(e).__name__)
        return ToolError(error=redact(str(e)), error_type=type(e).__name__).model_dump(mode="json")


@mcp.tool()
async def get_test_report(job_name: str, build_number: int) -> dict[str, Any]:
    """Return the Jenkins test report summary, when one is available."""
    try:
        client = _get_client()
        report = await client.get_test_report(job_name, build_number)
        return TestReportResult(
            evidence_id="test_report" if report else None,
            report=redact_value(report) if report else None,
        ).model_dump(mode="json")
    except Exception as e:
        logger.warning("get_test_report failed (%s)", type(e).__name__)
        return ToolError(error=redact(str(e)), error_type=type(e).__name__).model_dump(mode="json")


@mcp.tool()
async def get_build_history(job_name: str) -> dict[str, Any]:
    """Return up to 30 recent Jenkins build numbers, results, and timestamps."""
    try:
        client = _get_client()
        history = await client.get_build_history(job_name)
        safe_history = [
            BuildHistoryItem(
                number=item.get("number", 0),
                result=item.get("result"),
                timestamp=item.get("timestamp"),
                duration=item.get("duration"),
            )
            for item in history[:30]
            if "number" in item
        ]
        return BuildHistoryResult(
            evidence_id="build_history" if safe_history else None,
            builds=safe_history,
        ).model_dump(mode="json")
    except Exception as e:
        logger.warning("get_build_history failed (%s)", type(e).__name__)
        return ToolError(error=redact(str(e)), error_type=type(e).__name__).model_dump(mode="json")


@mcp.tool()
async def compare_with_last_success(job_name: str, build_number: int) -> dict[str, Any]:
    """Compare incident build with the most recent successful Jenkins build."""
    try:
        client = _get_client()
        current_info = await client.get_build(job_name, build_number)
        last_success = await client.get_previous_successful_build(job_name)
        last_success = redact_value(last_success)

        return CompareSuccessResult(
            evidence_id="last_success" if last_success else None,
            incident_commit=redact(str(current_info.get("git", {}).get("commit", ""))) or None,
            last_successful_build=last_success,
            last_success_commit=(
                last_success.get("actions", [{}])[0]
                .get("lastBuiltRevision", {})
                .get("SHA1")
                if isinstance(last_success, dict)
                else None
            ),
            same_commit=(
                current_info.get("actions", [{}])[0]
                .get("lastBuiltRevision", {})
                .get("SHA1")
                == last_success.get("actions", [{}])[0]
                .get("lastBuiltRevision", {})
                .get("SHA1")
                if isinstance(last_success, dict)
                else None
            ),
        ).model_dump(mode="json")
    except Exception as e:
        logger.warning("compare_with_last_success failed (%s)", type(e).__name__)
        return ToolError(error=redact(str(e)), error_type=type(e).__name__).model_dump(mode="json")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
