"""Analytics MCP Server (Owner: Person 1).

Exposes analytical calculations and database lookup tools via FastMCP.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from agents.jenkins_agent.history import find_related_incidents as find_related
from agents.jenkins_agent.parsing.normalization import compute_error_signature
from common.blackboard import FileBlackboard, get_blackboard
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
    message: str = "Flakiness history is unavailable."


class RelatedIncidentsResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    related_incident_ids: list[str] = Field(default_factory=list)
    # Matching is on the exact error signature, so every match scores 1.0.
    similarity_scores: list[float] = Field(default_factory=list)
    recurrence_count: int = 0
    cross_branch: bool = False
    regression_of_fixed_incident: bool = False
    related_incidents: list[dict[str, Any]] = Field(default_factory=list)
    message: str = "No earlier incident shares this error signature."


#: The blackboard holds each incident's evidence; the lookup scans it for the signature.
MAX_RELATED_INCIDENTS = 20
MAX_WINDOW_DAYS = 365


def _blackboard() -> FileBlackboard:
    return get_blackboard()


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
        logger.warning("Live flakiness check failed (%s)", type(exc).__name__)
        return FlakinessResult(
            test_name=safe_test_name,
            message="Flakiness history is unavailable: Jenkins could not be reached or is not configured.",
        ).model_dump(mode="json")


@mcp.tool()
async def find_related_incidents(
    error_signature: str,
    job_name: str = "target-app/main",
    window_days: int = 7,
    current_incident_id: str | None = None,
) -> dict[str, Any]:
    """Find earlier incidents that share this error signature within a time window.

    Reads the incident evidence on the Blackboard. Each match carries its status,
    whether it was on another branch, and whether it had already been fixed (a
    regression). `current_incident_id` is excluded from the search.
    """
    signature = (error_signature or "").strip()
    if not signature:
        return RelatedIncidentsResult(message="An error signature is required.").model_dump(mode="json")
    days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    try:
        found = await find_related(
            signature,
            job_name,
            window_days=days,
            current_incident_id=current_incident_id,
            bb=_blackboard(),
        )
    except Exception as exc:  # the tool must answer even if the blackboard is unreadable
        logger.warning("Related-incident lookup failed (%s)", type(exc).__name__)
        return RelatedIncidentsResult(message="Related-incident lookup is unavailable.").model_dump(mode="json")

    matches = sorted(found.related_incidents, key=lambda m: m.created_at or "", reverse=True)[:MAX_RELATED_INCIDENTS]
    count = found.recurrence_count
    message = (
        f"{count} earlier incident(s) share this error signature within {days} day(s)."
        if count
        else f"No earlier incident shares this error signature within {days} day(s)."
    )
    return RelatedIncidentsResult(
        related_incident_ids=[m.incident_id for m in matches],
        similarity_scores=[1.0 for _ in matches],
        recurrence_count=count,
        cross_branch=found.cross_branch,
        regression_of_fixed_incident=found.regression_of_fixed_incident,
        related_incidents=[{k: v for k, v in m.to_dict().items() if k != "error_signature"} for m in matches],
        message=message,
    ).model_dump(mode="json")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
