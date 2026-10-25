"""Tests for common/blackboard.py."""

import asyncio
from datetime import datetime, timezone
import pytest
from uuid import uuid4

from common.blackboard import (
    BlackboardEntry,
    BlackboardPermissionError,
    FileBlackboard,
    FileEvidenceStore,
    FailureWindowHint,
    MAX_PAYLOAD_BYTES,
)
from common.models import Evidence, EvidenceHypothesis, FailureTaxonomy


@pytest.fixture
def blackboard(tmp_path):
    return FileBlackboard(root=tmp_path / "blackboard")


def test_blackboard_post_and_read(blackboard):
    incident_id = uuid4()
    entry = BlackboardEntry(
        incident_id=incident_id,
        section="hints",
        key="jenkins_agent",
        author="jenkins_agent",
        payload={"failed_stage": "test", "stage": "Build"},
    )
    version = blackboard.post(entry)
    assert version == 1

    read_entry = blackboard.read(incident_id, section="hints", key="jenkins_agent")
    assert read_entry is not None
    assert read_entry.version == 1
    assert read_entry.payload["failed_stage"] == "test"


def test_blackboard_versioning_and_idempotency(blackboard):
    incident_id = uuid4()
    entry1 = BlackboardEntry(
        incident_id=incident_id,
        section="hints",
        key="jenkins_agent",
        author="jenkins_agent",
        payload={"data": "first"},
    )
    v1 = blackboard.post(entry1)
    assert v1 == 1

    # Idempotent re-post with identical payload returns existing version
    v1_repeat = blackboard.post(entry1)
    assert v1_repeat == 1

    # New payload increments version
    entry2 = BlackboardEntry(
        incident_id=incident_id,
        section="hints",
        key="jenkins_agent",
        author="jenkins_agent",
        payload={"data": "second"},
    )
    v2 = blackboard.post(entry2)
    assert v2 == 2

    # Verify reading latest vs specific version
    latest = blackboard.read(incident_id, section="hints", key="jenkins_agent", version="latest")
    assert latest.version == 2
    assert latest.payload["data"] == "second"

    ver1 = blackboard.read(incident_id, section="hints", key="jenkins_agent", version=1)
    assert ver1.version == 1
    assert ver1.payload["data"] == "first"


def test_blackboard_permission_errors(blackboard):
    incident_id = uuid4()

    # Jenkins agent trying to post to another agent's namespace
    bad_entry = BlackboardEntry(
        incident_id=incident_id,
        section="evidence",
        key="metrics_agent",
        author="jenkins_agent",
        payload={"info": "intruder"},
    )
    with pytest.raises(BlackboardPermissionError):
        blackboard.post(bad_entry)

    # Jenkins agent trying to post to synthesis section (coordinator only)
    bad_synthesis = BlackboardEntry(
        incident_id=incident_id,
        section="synthesis",
        key="summary",
        author="jenkins_agent",
        payload={"summary": "unauthorized"},
    )
    with pytest.raises(BlackboardPermissionError):
        blackboard.post(bad_synthesis)


def test_blackboard_redaction_applied(blackboard):
    incident_id = uuid4()
    entry = BlackboardEntry(
        incident_id=incident_id,
        section="hints",
        key="jenkins_agent",
        author="jenkins_agent",
        payload={"secret": "ghp_1234567890abcdef1234567890abcdef12345678"},
    )
    blackboard.post(entry)

    read_entry = blackboard.read(incident_id, section="hints", key="jenkins_agent")
    assert "ghp_1234567890abcdef1234567890abcdef12345678" not in read_entry.payload["secret"]
    assert "[REDACTED" in read_entry.payload["secret"]


@pytest.mark.asyncio
async def test_blackboard_wait_for(blackboard):
    incident_id = uuid4()

    # Test timeout when key does not exist
    result = await blackboard.wait_for(incident_id, section="hints", key="jenkins_agent", timeout_s=0.5)
    assert result is None

    # Test async wait succeeds when entry is written concurrently
    async def delayed_post():
        await asyncio.sleep(0.1)
        entry = BlackboardEntry(
            incident_id=incident_id,
            section="hints",
            key="jenkins_agent",
            author="jenkins_agent",
            payload={"ready": True},
        )
        blackboard.post(entry)

    asyncio.create_task(delayed_post())
    found = await blackboard.wait_for(incident_id, section="hints", key="jenkins_agent", timeout_s=2.0)
    assert found is not None
    assert found.payload["ready"] is True


def test_blackboard_history_provenance(blackboard):
    incident_id = uuid4()
    entry1 = BlackboardEntry(
        incident_id=incident_id,
        section="hints",
        key="jenkins_agent",
        author="jenkins_agent",
        payload={"step": 1},
    )
    blackboard.post(entry1)

    entry2 = BlackboardEntry(
        incident_id=incident_id,
        section="hints",
        key="jenkins_agent",
        author="jenkins_agent",
        payload={"step": 2},
    )
    blackboard.post(entry2)

    history = blackboard.history(incident_id)
    assert len(history) == 2
    assert history[0].version == 1
    assert history[1].version == 2


@pytest.mark.asyncio
async def test_evidence_store_compatibility_alias(blackboard):
    store = FileEvidenceStore(blackboard.root)
    incident_id = uuid4()
    evidence = Evidence(
        incident_id=incident_id,
        agent="jenkins_agent",
        created_at=datetime.now(timezone.utc),
        status="completed",
        failure_type=FailureTaxonomy.BUILD_COMPILATION_FAILURE,
        summary="Compilation error",
        confidence=0.9,
        root_cause_hypotheses=[
            EvidenceHypothesis(
                hypothesis="Syntax error in main.py",
                failure_type=FailureTaxonomy.BUILD_COMPILATION_FAILURE,
                confidence=0.9,
                supporting_evidence=[],
            )
        ],
        redaction_applied=True,
    )

    await store.write(evidence)
    read_back = await store.read(incident_id, "jenkins_agent")
    assert read_back is not None
    assert read_back.agent == "jenkins_agent"
    assert read_back.root_cause_hypotheses[0].failure_type == FailureTaxonomy.BUILD_COMPILATION_FAILURE
