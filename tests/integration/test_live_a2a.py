"""The Coordinator service over a real HTTP A2A transport, driven by the agents' real notifier.

Still one process on localhost (no containers), but the transport is genuine: an uvicorn
server, the A2A JSON-RPC endpoint, and ``A2ANotifier`` as the client the agents use. What it
proves: a notification travels, the Coordinator reads the blackboard and writes a report,
and a missing agent produces an incomplete report on timeout that a late agent then
upgrades. What it does not prove: separate containers or the Docker network.
"""

import asyncio
import socket

import pytest
import pytest_asyncio
import uvicorn

from common.a2a_contract import evidence_ready
from common.audit import FileAuditLog
from common.evidence_store import FileEvidenceStore
from common.models import FailureTaxonomy
from coordinator.a2a_client import A2ANotifier, notifier_from_env
from coordinator.agent import CoordinatorAgent
from coordinator.fused import synthesize_fused
from coordinator.report import Completeness
from coordinator.report_store import ReportStore
from coordinator.service import build_app
from tests.coordinator.helpers import INCIDENT, T, make

pytestmark = pytest.mark.asyncio


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def service(tmp_path):
    store = FileEvidenceStore(tmp_path / "evidence")
    reports = ReportStore(tmp_path / "reports")
    audit = FileAuditLog(tmp_path / "audit")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    async def synthesize(incident_id):
        return await synthesize_fused(store, incident_id, report_store=reports, audit=audit)

    app = build_app(CoordinatorAgent(store), url, 0.6, synthesize)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield url, store, reports, audit
    server.should_exit = True
    await task


async def publish(store, notifier, agent, failure_type=T.TIMEOUT, confidence=0.8):
    await store.write(make(agent, failure_type, confidence))
    await notifier(evidence_ready(INCIDENT, agent))


async def test_all_three_agents_notify_and_a_complete_report_is_written(service):
    url, store, reports, audit = service
    notifier = A2ANotifier(url)
    for agent in ("jenkins_agent", "metrics_agent", "code_agent"):
        await publish(store, notifier, agent)

    report = await reports.read(INCIDENT)
    assert report is not None
    assert report.verdict == FailureTaxonomy.TIMEOUT and report.completeness == Completeness.COMPLETE
    events = [r.event_type for r in await audit.read(INCIDENT)]
    assert events.count("synthesis_completed") == 1


async def test_missing_agent_times_out_to_an_incomplete_report_and_a_late_agent_upgrades_it(service):
    url, store, reports, _audit = service
    notifier = A2ANotifier(url)
    await publish(store, notifier, "jenkins_agent")
    await publish(store, notifier, "metrics_agent")
    assert await reports.read(INCIDENT) is None  # still waiting

    await asyncio.sleep(1.0)  # past the 0.6 s timeout
    incomplete = await reports.read(INCIDENT)
    assert incomplete is not None
    assert incomplete.completeness == Completeness.INCOMPLETE and incomplete.missing_agents == ["code_agent"]
    assert not incomplete.ready_for_policy_review

    await publish(store, notifier, "code_agent")
    final = await reports.read(INCIDENT)
    assert final.completeness == Completeness.COMPLETE and final.missing_agents == []


async def test_duplicate_notifications_do_not_resynthesize(service):
    url, store, _reports, audit = service
    notifier = A2ANotifier(url)
    for agent in ("jenkins_agent", "metrics_agent", "code_agent"):
        await publish(store, notifier, agent)
    await notifier(evidence_ready(INCIDENT, "metrics_agent"))
    events = [r.event_type for r in await audit.read(INCIDENT)]
    assert events.count("synthesis_completed") == 1


async def test_a_malformed_notification_is_rejected_and_writes_nothing(service):
    url, _store, reports, _audit = service
    from a2a.types import Message, Part, Role

    bad = Message(message_id="m1", context_id=str(INCIDENT), role=Role.ROLE_AGENT)
    bad.parts.append(Part(text="hi"))
    bad.metadata["kind"] = "something.else"
    await A2ANotifier(url)(bad)  # accepted by the transport, rejected by the contract
    assert await reports.read(INCIDENT) is None


async def test_notifier_is_disabled_without_a_coordinator_url(monkeypatch):
    monkeypatch.delenv("COORDINATOR_URL", raising=False)
    assert notifier_from_env() is None
    monkeypatch.setenv("COORDINATOR_URL", "http://coordinator:8090")
    assert isinstance(notifier_from_env(), A2ANotifier)


async def test_an_unreachable_coordinator_raises_so_the_agents_best_effort_wrapper_can_log_it():
    with pytest.raises(Exception):  # noqa: B017 - any transport error is acceptable
        await A2ANotifier(f"http://127.0.0.1:{free_port()}", timeout=2)(evidence_ready(INCIDENT, "metrics_agent"))
