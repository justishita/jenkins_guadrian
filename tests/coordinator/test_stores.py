"""The Coordinator must see evidence wherever an agent wrote it.

Regression for the smoke-test finding: the Jenkins agent writes to the blackboard, the Metrics and
Code agents to the evidence directory, and a Coordinator reading only the evidence directory reported
Jenkins as missing even though its A2A task had completed.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from common.blackboard import EvidenceStore as BlackboardEvidenceStore
from common.blackboard import FileBlackboard
from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy
from coordinator.agent import CoordinatorAgent
from coordinator.stores import MergedEvidenceStore

pytestmark = pytest.mark.asyncio


def evidence(incident, agent, failure_type=FailureTaxonomy.TIMEOUT, confidence=0.8, created_at=None) -> Evidence:
    return Evidence(
        incident_id=incident,
        agent=agent,
        created_at=created_at or datetime.now(timezone.utc),
        status="completed",
        failure_type=failure_type,
        summary=f"{agent} finding",
        confidence=confidence,
        redaction_applied=True,
    )


@pytest.fixture
def stores(tmp_path):
    blackboard = BlackboardEvidenceStore(FileBlackboard(tmp_path / "blackboard"))
    files = FileEvidenceStore(tmp_path / "evidence")
    return blackboard, files, MergedEvidenceStore(blackboard, files)


async def test_evidence_from_either_store_is_visible(stores):
    blackboard, files, merged = stores
    incident = uuid4()
    await blackboard.write(evidence(incident, "jenkins_agent"))  # the Jenkins agent's store
    await files.write(evidence(incident, "metrics_agent"))  # the Metrics/Code agents' store
    assert [e.agent for e in await merged.read_all(incident)] == ["jenkins_agent", "metrics_agent"]


async def test_the_newest_document_per_agent_wins(stores):
    blackboard, files, merged = stores
    incident = uuid4()
    now = datetime.now(timezone.utc)
    await files.write(evidence(incident, "code_agent", confidence=0.3, created_at=now - timedelta(minutes=5)))
    await blackboard.write(evidence(incident, "code_agent", confidence=0.9, created_at=now))
    [item] = await merged.read_all(incident)
    assert item.confidence == 0.9
    assert (await merged.read(incident, "code_agent")).confidence == 0.9
    assert await merged.read(incident, "metrics_agent") is None


async def test_an_agent_in_both_stores_is_counted_once(stores):
    blackboard, files, merged = stores
    incident = uuid4()
    same = evidence(incident, "metrics_agent")
    await blackboard.write(same)
    await files.write(same)
    assert len(await merged.read_all(incident)) == 1


async def test_one_unreadable_store_does_not_hide_the_other(stores):
    blackboard, _files, _merged = stores
    incident = uuid4()

    class Broken:
        async def read_all(self, _incident):
            raise OSError("disk gone")

    await blackboard.write(evidence(incident, "jenkins_agent"))
    assert [e.agent for e in await MergedEvidenceStore(Broken(), blackboard).read_all(incident)] == ["jenkins_agent"]


async def test_the_coordinator_reports_a_blackboard_only_agent_as_present(stores):
    blackboard, files, merged = stores
    incident = uuid4()
    await blackboard.write(evidence(incident, "jenkins_agent"))
    await files.write(evidence(incident, "metrics_agent"))
    await files.write(evidence(incident, "code_agent"))
    view = await CoordinatorAgent(merged).collect(incident)
    assert view.complete and view.missing_agents == ()
    assert view.reporting_agents == ("code_agent", "jenkins_agent", "metrics_agent")


async def test_with_only_the_evidence_directory_the_blackboard_agent_would_be_missing(stores):
    """The bug this module fixes, pinned: a one-store coordinator sees Jenkins as missing."""
    blackboard, files, _merged = stores
    incident = uuid4()
    await blackboard.write(evidence(incident, "jenkins_agent"))
    await files.write(evidence(incident, "metrics_agent"))
    await files.write(evidence(incident, "code_agent"))
    assert (await CoordinatorAgent(files).collect(incident)).missing_agents == ("jenkins_agent",)


class BrokenStore:
    async def read_all(self, incident_id):
        raise OSError("disk gone")


async def test_an_unreadable_store_is_reported_not_mistaken_for_an_absent_agent(stores, caplog):
    blackboard, files, _ = stores
    merged = MergedEvidenceStore(BrokenStore(), files)
    incident = uuid4()
    await files.write(evidence(incident, "metrics_agent"))
    with caplog.at_level("WARNING"):
        found = await merged.read_all(incident)
    assert [e.agent for e in found] == ["metrics_agent"]  # the healthy store still answers
    assert merged.read_errors[str(incident)] == ["BrokenStore: OSError"]
    assert any("unreadable" in message for message in caplog.messages)


async def test_read_errors_are_cleared_when_the_stores_recover(stores):
    blackboard, files, _ = stores
    incident = uuid4()
    merged = MergedEvidenceStore(BrokenStore(), files)
    await merged.read_all(incident)
    assert merged.read_errors[str(incident)]
    merged._stores = (files,)
    await merged.read_all(incident)
    assert merged.read_errors[str(incident)] == []
