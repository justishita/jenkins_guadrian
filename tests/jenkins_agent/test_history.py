"""Tests for related-incident lookup, deduplication, and confidence boost."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest

from agents.jenkins_agent.history import (
    compute_preliminary_fingerprint,
    find_related_incidents,
    RelatedIncidentResult,
)
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import FailureTaxonomy


@pytest.fixture
def blackboard(tmp_path):
    return FileBlackboard(tmp_path / "blackboard")


@pytest.mark.asyncio
async def test_preliminary_fingerprint_deterministic():
    fp1 = compute_preliminary_fingerprint("target-app/main", "Test")
    fp2 = compute_preliminary_fingerprint("target-app/main", "Test")
    fp3 = compute_preliminary_fingerprint("target-app/feature", "Test")
    assert fp1 == fp2
    assert fp1 != fp3


@pytest.mark.asyncio
async def test_find_related_incidents_empty_signature(blackboard):
    res = await find_related_incidents("", "target-app/main", bb=blackboard)
    assert res.recurrence_count == 0
    assert res.related_incident_ids == []
    assert res.history_consistency_boost == 0.0


@pytest.mark.asyncio
async def test_find_related_incidents_matches_prior_and_boosts_confidence(blackboard):
    prior_id = uuid4()
    error_sig = "sig_1234567890abcdef"
    
    # Post prior incident evidence to blackboard
    blackboard.post(
        BlackboardEntry(
            incident_id=prior_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": "target-app/main",
                "branch": "main",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    blackboard.post(
        BlackboardEntry(
            incident_id=prior_id,
            section="evidence",
            key="jenkins_agent",
            author="jenkins_agent",
            payload={
                "status": "completed",
                "failure_type": "code_test_failure",
                "error_signature": error_sig,
                "confidence": 0.9,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    )

    current_id = uuid4()
    result = await find_related_incidents(
        error_signature=error_sig,
        job_name="target-app/main",
        current_incident_id=current_id,
        current_failure_type="code_test_failure",
        bb=blackboard,
    )

    assert result.recurrence_count == 1
    assert str(prior_id) in result.related_incident_ids
    assert result.confirmed_correct is True
    assert result.history_consistency_boost == 0.10
    assert result.cross_branch is False
    assert result.regression_of_fixed_incident is False


@pytest.mark.asyncio
async def test_find_related_incidents_cross_branch_detection(blackboard):
    prior_id = uuid4()
    error_sig = "sig_cross_branch"

    blackboard.post(
        BlackboardEntry(
            incident_id=prior_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": "target-app/main",
                "branch": "main",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    blackboard.post(
        BlackboardEntry(
            incident_id=prior_id,
            section="evidence",
            key="jenkins_agent",
            author="jenkins_agent",
            payload={
                "status": "completed",
                "failure_type": "code_test_failure",
                "error_signature": error_sig,
                "confidence": 0.85,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    )

    current_id = uuid4()
    # Different branch: feature/branch-x
    result = await find_related_incidents(
        error_signature=error_sig,
        job_name="target-app/feature/branch-x",
        current_incident_id=current_id,
        current_failure_type="code_test_failure",
        current_branch="feature/branch-x",
        bb=blackboard,
    )

    assert result.recurrence_count == 1
    assert result.cross_branch is True
    assert result.related_incidents[0].cross_branch is True


@pytest.mark.asyncio
async def test_find_related_incidents_regression_of_fixed_incident(blackboard):
    prior_id = uuid4()
    error_sig = "sig_fixed_recurrence"

    blackboard.post(
        BlackboardEntry(
            incident_id=prior_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": "target-app/main",
                "branch": "main",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    blackboard.post(
        BlackboardEntry(
            incident_id=prior_id,
            section="evidence",
            key="jenkins_agent",
            author="jenkins_agent",
            payload={
                "status": "resolved",
                "resolution": "fixed",
                "failure_type": "code_test_failure",
                "error_signature": error_sig,
                "confidence": 0.9,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    )

    current_id = uuid4()
    result = await find_related_incidents(
        error_signature=error_sig,
        job_name="target-app/main",
        current_incident_id=current_id,
        current_failure_type="code_test_failure",
        bb=blackboard,
    )

    assert result.recurrence_count == 1
    assert result.regression_of_fixed_incident is True
    assert result.related_incidents[0].regression_of_fixed_incident is True


@pytest.mark.asyncio
async def test_find_related_incidents_window_cutoff(blackboard):
    old_id = uuid4()
    error_sig = "sig_too_old"
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)

    blackboard.post(
        BlackboardEntry(
            incident_id=old_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": "target-app/main",
                "branch": "main",
                "timestamp": eight_days_ago.isoformat(),
            },
        )
    )
    blackboard.post(
        BlackboardEntry(
            incident_id=old_id,
            section="evidence",
            key="jenkins_agent",
            author="jenkins_agent",
            payload={
                "status": "completed",
                "failure_type": "code_test_failure",
                "error_signature": error_sig,
                "confidence": 0.9,
                "created_at": eight_days_ago.isoformat(),
            },
        )
    )

    current_id = uuid4()
    result = await find_related_incidents(
        error_signature=error_sig,
        job_name="target-app/main",
        current_incident_id=current_id,
        window_days=7,
        bb=blackboard,
    )

    # 8 days ago is outside 7 days window
    assert result.recurrence_count == 0
    assert result.related_incident_ids == []
