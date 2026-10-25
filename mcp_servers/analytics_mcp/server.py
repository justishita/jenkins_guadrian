"""Analytics MCP Server (Owner: Person 1).

Exposes analytical calculations and database lookup tools via FastMCP.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from agents.jenkins_agent.parsing.normalization import compute_error_signature
from common.redaction import redact

logger = logging.getLogger(__name__)

mcp = FastMCP("analytics_mcp")
mcp.settings.host = "0.0.0.0"
mcp.settings.port = 8082


class ErrorSignatureResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error_signature: str


class FlakinessResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    test_name: str
    is_flaky: bool = False
    flakiness_score: float = 0.0
    history_runs: int = 0
    message: str = "Flakiness detection stub (Week 5)"


class RelatedIncidentsResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    related_incident_ids: list[str] = Field(default_factory=list)
    similarity_scores: list[float] = Field(default_factory=list)
    message: str = "Related incident search stub (Week 5)"


@mcp.tool()
def compute_signature(failed_stage: str | None, error_blocks: list[str]) -> dict[str, Any]:
    """Compute deterministic SHA-256 error signature from failed stage and error blocks."""
    sig = compute_error_signature(failed_stage or "", error_blocks)
    return ErrorSignatureResult(error_signature=sig).model_dump(mode="json")


@mcp.tool()
async def check_flakiness(test_name: str, job: str = "target-app/main") -> dict[str, Any]:
    """Check whether a test is known to be flaky across recent pipeline runs."""
    safe_test_name = redact(test_name)
    try:
        from agents.jenkins_agent.tools.flaky import analyze_test_flakiness
        from agents.jenkins_agent.tools.jenkins_client import JenkinsClient

        async with JenkinsClient() as client:
            report = await analyze_test_flakiness(job, safe_test_name, client)
            return FlakinessResult(
                test_name=safe_test_name,
                is_flaky=report.is_flaky,
                flakiness_score=report.flaky_score,
                history_runs=report.total_runs,
                message=report.details,
            ).model_dump(mode="json")
    except Exception as exc:
        logger.debug("Live flakiness check failed: %s", exc)
        return FlakinessResult(test_name=safe_test_name).model_dump(mode="json")


@mcp.tool()
def find_related_incidents(error_signature: str) -> dict[str, Any]:
    """Find historically related incidents sharing the error signature."""
    return RelatedIncidentsResult().model_dump(mode="json")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
