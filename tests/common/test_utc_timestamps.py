"""Boundary timestamps must be timezone-aware and normalized to UTC."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from common.audit import AuditRecord
from common.blackboard import BlackboardEntry
from common.models import Evidence, IncidentCreatedEvent


def test_incident_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timestamp must include a timezone"):
        IncidentCreatedEvent(
            incident_id=uuid4(),
            job_name="job",
            build_number=1,
            build_url="http://jenkins/job/1",
            branch="main",
            git_commit="abc",
            timestamp=datetime(2026, 1, 1),
        )


def test_aware_timestamps_are_normalized_to_utc() -> None:
    timestamp = datetime(
        2026,
        1,
        1,
        3,
        tzinfo=timezone(timedelta(hours=3)),
    )
    event = IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="job",
        build_number=1,
        build_url="http://jenkins/job/1",
        branch="main",
        git_commit="abc",
        timestamp=timestamp,
    )

    assert event.timestamp == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_evidence_and_audit_records_reject_naive_creation_times() -> None:
    with pytest.raises(ValidationError, match="created_at must include a timezone"):
        Evidence(
            incident_id=uuid4(),
            created_at=datetime(2026, 1, 1),
            status="completed",
            failure_type="unknown",
            summary="No evidence",
            confidence=0.1,
            redaction_applied=True,
        )

    with pytest.raises(ValidationError, match="created_at must include a timezone"):
        AuditRecord(
            incident_id=uuid4(),
            actor="test",
            event_type="test",
            created_at=datetime(2026, 1, 1),
        )


def test_blackboard_entry_rejects_naive_creation_time() -> None:
    with pytest.raises(ValidationError, match="created_at must include a timezone"):
        BlackboardEntry(
            incident_id=uuid4(),
            section="context",
            key="event",
            author="backend",
            payload={},
            created_at=datetime(2026, 1, 1),
        )
