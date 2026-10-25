"""Related-incident lookup for Jenkins build failure deduplication.

Queries the evidence store (Blackboard / PostgreSQL) for prior incidents sharing
the same ``error_signature`` within a configurable time window (default 7 days).
Produces a ``RelatedIncidentResult`` that the agent attaches to its evidence so the
Coordinator can leverage recurrence patterns during synthesis.

Key requirements (Prompt 5.2):
- find_related_incidents(error_signature, job_name, window_days=7)
- Returns prior incident ids with their status and resolution.
- If a match exists:
    - related_incident_ids
    - recurrence_count
    - boosts confidence (history_consistency) if the earlier classification was confirmed correct
- Cross-branch matches: relate but tag cross_branch=True
- Resolved incidents that recur: flag 'regression_of_fixed_incident'
- Race conditions when two identical failures arrive at once: DB unique constraint / advisory lock
- Never silently drop the new incident; always create it and LINK it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

from common.blackboard import FileBlackboard, get_blackboard
from common.fingerprint import compute_preliminary_fingerprint  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 7
HISTORY_CONSISTENCY_BOOST = 0.10
RESOLVED_STATUSES = frozenset({"RESOLVED", "CLOSED", "FIXED", "REMEDIATED"})

# In-process lock / advisory guard
_DEDUP_LOCK = asyncio.Lock()




@dataclass
class RelatedIncident:
    """Prior incident matching the current failure's error signature."""

    incident_id: str
    status: str
    resolution: str | None = None
    failure_type: str | None = None
    confidence: float | None = None
    error_signature: str = ""
    job_name: str = ""
    branch: str | None = None
    created_at: str | None = None
    cross_branch: bool = False
    regression_of_fixed_incident: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "status": self.status,
            "resolution": self.resolution,
            "failure_type": self.failure_type,
            "confidence": self.confidence,
            "error_signature": self.error_signature,
            "job_name": self.job_name,
            "branch": self.branch,
            "created_at": self.created_at,
            "cross_branch": self.cross_branch,
            "regression_of_fixed_incident": self.regression_of_fixed_incident,
        }


@dataclass
class RelatedIncidentResult:
    """Aggregated result of related-incident search."""

    related_incident_ids: list[str] = field(default_factory=list)
    related_incidents: list[RelatedIncident] = field(default_factory=list)
    recurrence_count: int = 0
    history_consistency_boost: float = 0.0
    confirmed_correct: bool = False
    cross_branch: bool = False
    regression_of_fixed_incident: bool = False
    preliminary_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "related_incident_ids": self.related_incident_ids,
            "related_incidents": [r.to_dict() for r in self.related_incidents],
            "recurrence_count": self.recurrence_count,
            "history_consistency_boost": round(self.history_consistency_boost, 4),
            "confirmed_correct": self.confirmed_correct,
            "cross_branch": self.cross_branch,
            "regression_of_fixed_incident": self.regression_of_fixed_incident,
            "preliminary_fingerprint": self.preliminary_fingerprint,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _extract_branch(job_name: str, branch: str | None = None) -> str:
    if branch:
        return branch.strip()
    if "/" in job_name:
        return job_name.split("/")[-1].strip()
    return ""


def _is_cross_branch(current_job: str, prior_job: str, current_branch: str | None = None, prior_branch: str | None = None) -> bool:
    """Determine if failure is from a different branch."""
    b_curr = _extract_branch(current_job, current_branch)
    b_prior = _extract_branch(prior_job, prior_branch)
    if b_curr and b_prior and b_curr.lower() != b_prior.lower():
        return True
    return False


def _search_blackboard(
    bb: FileBlackboard,
    error_signature: str,
    job_name: str,
    current_incident_id: str,
    window_cutoff: datetime,
    current_failure_type: str | None = None,
    current_branch: str | None = None,
) -> list[RelatedIncident]:
    """Scan Blackboard evidence and context entries for matching error_signature."""
    related: list[RelatedIncident] = []
    root = bb.root
    if not root.exists():
        return related

    for incident_dir in sorted(root.iterdir()):
        if not incident_dir.is_dir():
            continue
        inc_id = incident_dir.name
        if inc_id == current_incident_id:
            continue

        history_file = incident_dir / "history.jsonl"
        if not history_file.exists():
            continue

        prior_sig: str | None = None
        prior_status: str = "OPEN"
        prior_resolution: str | None = None
        prior_failure_type: str | None = None
        prior_confidence: float | None = None
        prior_job_name: str = ""
        prior_branch: str | None = None
        prior_created_at: datetime | None = None

        try:
            with open(history_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    section = entry.get("section", "")
                    payload = entry.get("payload", {})
                    if not isinstance(payload, dict):
                        continue

                    if section == "context":
                        prior_job_name = payload.get("job_name", prior_job_name)
                        prior_branch = payload.get("branch", prior_branch)
                        raw_ts = payload.get("timestamp") or payload.get("received_at")
                        if raw_ts and prior_created_at is None:
                            try:
                                dt = datetime.fromisoformat(str(raw_ts))
                                prior_created_at = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                            except (ValueError, TypeError):
                                pass

                    if section == "evidence" and entry.get("key") == "jenkins_agent":
                        prior_sig = payload.get("error_signature", prior_sig)
                        prior_status = payload.get("status", prior_status)
                        prior_failure_type = payload.get("failure_type", prior_failure_type)
                        prior_confidence = payload.get("confidence", prior_confidence)
                        prior_resolution = payload.get("resolution", prior_resolution)
                        raw_ca = payload.get("created_at")
                        if raw_ca and prior_created_at is None:
                            try:
                                dt = datetime.fromisoformat(str(raw_ca))
                                prior_created_at = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                            except (ValueError, TypeError):
                                pass

                    if section == "hints" and "error_signature" in payload:
                        if prior_sig is None:
                            prior_sig = payload.get("error_signature")

                    if section == "approval" or section == "validation":
                        if payload.get("status"):
                            prior_status = payload.get("status")
                        if payload.get("resolution"):
                            prior_resolution = payload.get("resolution")

        except Exception as exc:
            logger.debug("Failed reading history for incident %s: %s", inc_id, exc)
            continue

        if not prior_sig or prior_sig.strip() != error_signature.strip():
            continue

        if prior_created_at is not None and prior_created_at < window_cutoff:
            continue

        is_cross = _is_cross_branch(job_name, prior_job_name, current_branch, prior_branch)
        is_fixed = (
            str(prior_status).upper() in RESOLVED_STATUSES
            or (prior_resolution and str(prior_resolution).upper() in RESOLVED_STATUSES)
        )

        related.append(
            RelatedIncident(
                incident_id=inc_id,
                status=prior_status,
                resolution=prior_resolution,
                failure_type=prior_failure_type,
                confidence=prior_confidence,
                error_signature=prior_sig,
                job_name=prior_job_name,
                branch=prior_branch,
                created_at=prior_created_at.isoformat() if prior_created_at else None,
                cross_branch=is_cross,
                regression_of_fixed_incident=is_fixed,
            )
        )

    return related


async def find_related_incidents(
    error_signature: str,
    job_name: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    current_incident_id: str | UUID | None = None,
    current_failure_type: str | None = None,
    current_branch: str | None = None,
    bb: FileBlackboard | None = None,
    failed_stage: str | None = None,
) -> RelatedIncidentResult:
    """Find prior incidents matching the same error_signature within window_days.

    Args:
        error_signature: Normalized hash/signature from Jenkins log parsing.
        job_name: Current job name.
        window_days: Cutoff window in days (default 7).
        current_incident_id: Incident ID to exclude from search.
        current_failure_type: Classified failure type to check if previous classification confirmed correct.
        current_branch: Branch of the current build.
        bb: Optional Blackboard instance.
        failed_stage: Optional failed stage name for preliminary fingerprinting.

    Returns:
        RelatedIncidentResult with list of related incidents, recurrence_count, boost, flags.
    """
    preliminary_fp = compute_preliminary_fingerprint(job_name, failed_stage)
    if not error_signature or not error_signature.strip():
        return RelatedIncidentResult(preliminary_fingerprint=preliminary_fp)

    window_cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    blackboard = bb or get_blackboard()
    cur_id_str = str(current_incident_id) if current_incident_id else ""

    async with _DEDUP_LOCK:
        matches = await asyncio.to_thread(
            _search_blackboard,
            blackboard,
            error_signature,
            job_name,
            cur_id_str,
            window_cutoff,
            current_failure_type,
            current_branch,
        )

    related_ids = [m.incident_id for m in matches]
    recurrence_count = len(matches)
    has_cross_branch = any(m.cross_branch for m in matches)
    has_regression_of_fixed = any(m.regression_of_fixed_incident for m in matches)

    confirmed_correct = False
    boost = 0.0
    if matches and current_failure_type:
        for m in matches:
            # Check if earlier classification was confirmed correct (e.g. status completed/resolved or high confidence match)
            if m.failure_type == current_failure_type and m.status.upper() not in {"FAILED", "UNKNOWN"}:
                confirmed_correct = True
                boost = HISTORY_CONSISTENCY_BOOST
                break

    return RelatedIncidentResult(
        related_incident_ids=related_ids,
        related_incidents=matches,
        recurrence_count=recurrence_count,
        history_consistency_boost=boost,
        confirmed_correct=confirmed_correct,
        cross_branch=has_cross_branch,
        regression_of_fixed_incident=has_regression_of_fixed,
        preliminary_fingerprint=preliminary_fp,
    )
