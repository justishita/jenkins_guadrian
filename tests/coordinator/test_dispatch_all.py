"""Coordinator fan-out: every required agent gets an explicit outcome, then one synthesis.

**Owner:** P2 (Week 5). Covers ``CoordinatorRuntime.dispatch_all``: A2A agents are triggered
with the shared retry policy, self-triggering agents are watched on the blackboard with a
deadline, a failed or timed-out agent makes the report incomplete (and so not actionable),
and a redelivered message never investigates or synthesizes twice.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent
from coordinator.agent import CoordinatorAgent
from coordinator.app import AgentOutcome, CoordinatorRuntime

pytestmark = pytest.mark.asyncio

DEP = FailureTaxonomy.DEPENDENCY_REGRESSION


def make_event(attempt: int = 0) -> IncidentCreatedEvent:
    return IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="target-app",
        build_number=3,
        build_url="http://jenkins:8080/job/target-app/3/",
        branch="main",
        git_commit="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        timestamp=datetime.now(timezone.utc),
        remediation_attempt=attempt,
    )


def evidence(event: IncidentCreatedEvent, agent: str, failure_type=DEP, confidence=0.8, status="completed") -> Evidence:
    return Evidence(
        incident_id=event.incident_id,
        agent=agent,  # type: ignore[arg-type]
        created_at=datetime.now(timezone.utc),
        status=status,  # type: ignore[arg-type]
        failure_type=failure_type,
        summary=f"{agent} finding",
        confidence=confidence,
        redaction_applied=True,
    )


def ok(event: IncidentCreatedEvent, agent: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "incident_id": str(event.incident_id),
            "attempt": event.remediation_attempt,
            "blackboard_ref": {"incident_id": str(event.incident_id), "section": "evidence", "key": agent, "version": 1},
        },
    )


class Harness:
    """A runtime wired to a mock A2A network and a temp evidence store."""

    def __init__(self, tmp_path: Path, event: IncidentCreatedEvent, routes: dict[str, object]) -> None:
        self.store = FileEvidenceStore(tmp_path / "evidence")
        self.requests: list[httpx.Request] = []
        self.synthesized = 0
        runtime = CoordinatorRuntime()
        runtime.adk_agent = CoordinatorAgent(self.store)
        runtime.a2a_agents = ("jenkins_agent", "metrics_agent")
        runtime.watch_timeout_seconds = 0.05
        runtime.watch_poll_seconds = 0.01

        original = runtime.adk_agent.synthesize

        async def counting(*args, **kwargs):
            self.synthesized += 1
            return await original(*args, **kwargs)

        object.__setattr__(runtime.adk_agent, "synthesize", counting)

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            for agent, url in runtime.agent_urls.items():
                if str(request.url).startswith(url):
                    route = routes[agent]
                    if isinstance(route, Exception):
                        raise route
                    return route(event) if callable(route) else route
            raise AssertionError(f"unexpected request {request.url}")

        runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.runtime = runtime

    async def close(self) -> None:
        await self.runtime.client.aclose()


@pytest.fixture
def no_sleep(monkeypatch):
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("coordinator.app.asyncio.sleep", instant)


def report_of(harness: Harness, event: IncidentCreatedEvent):
    return harness.runtime.reports[str(event.incident_id)]


async def test_all_three_agents_complete_gives_one_complete_actionable_report(tmp_path, no_sleep):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    for agent in ("jenkins_agent", "metrics_agent", "code_agent"):
        await h.store.write(evidence(event, agent, DEP if agent != "metrics_agent" else FailureTaxonomy.UNKNOWN, 0.8))
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()

    assert outcomes == {a: "completed" for a in ("jenkins_agent", "metrics_agent", "code_agent")}
    assert h.synthesized == 1
    report = report_of(h, event)
    assert report.complete and report.actionable and report.agent_outcomes == outcomes
    assert h.runtime.outcomes[str(event.incident_id)] == outcomes


async def test_a2a_agents_are_sent_authenticated_tasks_and_the_watched_agent_is_not(tmp_path, no_sleep):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    await h.store.write(evidence(event, "code_agent"))
    try:
        await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    paths = sorted(str(r.url) for r in h.requests)
    assert paths == [f"{h.runtime.agent_urls['jenkins_agent']}/tasks", f"{h.runtime.agent_urls['metrics_agent']}/tasks"]


async def test_a_failed_agent_makes_the_report_incomplete_and_not_actionable(tmp_path, no_sleep):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": httpx.Response(500, json={})})
    await h.store.write(evidence(event, "jenkins_agent"))
    await h.store.write(evidence(event, "code_agent", confidence=0.9))
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()

    assert outcomes["metrics_agent"] == "failed"
    report = report_of(h, event)
    assert report.status == "resolved_cause" and report.confidence >= 0.5  # a cause was named
    assert not report.complete and not report.actionable


async def test_a_504_is_timed_out_without_retrying(tmp_path, no_sleep):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": httpx.Response(504, json={})})
    await h.store.write(evidence(event, "code_agent"))
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    assert outcomes["metrics_agent"] == "timed_out"
    assert sum(1 for r in h.requests if str(r.url).startswith(h.runtime.agent_urls["metrics_agent"])) == 1


async def test_a_client_timeout_is_timed_out_after_the_retries(tmp_path, no_sleep):
    event = make_event()
    h = Harness(
        tmp_path, event,
        {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": httpx.ReadTimeout("slow")},
    )
    await h.store.write(evidence(event, "code_agent"))
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    assert outcomes["metrics_agent"] == "timed_out"
    assert sum(1 for r in h.requests if str(r.url).startswith(h.runtime.agent_urls["metrics_agent"])) == 3


async def test_a_5xx_is_retried_and_can_recover(tmp_path, no_sleep):
    event = make_event()
    calls = {"n": 0}

    def flaky(e):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else ok(e, "metrics_agent")

    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": flaky})
    await h.store.write(evidence(event, "code_agent"))
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    assert outcomes["metrics_agent"] == "completed" and calls["n"] == 2


async def test_a_response_for_the_wrong_attempt_is_a_failure(tmp_path, no_sleep):
    event = make_event(attempt=1)

    def stale(e):
        return httpx.Response(
            200,
            json={"status": "completed", "incident_id": str(e.incident_id), "attempt": 0, "blackboard_ref": {}},
        )

    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": stale})
    await h.store.write(evidence(event, "code_agent"))
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    assert outcomes["metrics_agent"] == "failed"


async def test_a_watched_agent_that_never_reports_times_out_instead_of_waiting_forever(tmp_path):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    await h.store.write(evidence(event, "jenkins_agent"))
    try:
        outcomes = await h.runtime.dispatch_all(event)  # code_agent never writes
    finally:
        await h.close()
    assert outcomes["code_agent"] == "timed_out"
    report = report_of(h, event)
    assert "code_agent" in report.missing_agents and not report.complete and not report.actionable


async def test_a_watched_agent_that_reports_late_but_in_time_completes(tmp_path):
    import asyncio

    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    h.runtime.watch_timeout_seconds = 2.0
    await h.store.write(evidence(event, "jenkins_agent"))

    async def late_write() -> None:
        await asyncio.sleep(0.1)
        await h.store.write(evidence(event, "code_agent"))

    try:
        writer = asyncio.create_task(late_write())
        outcomes = await h.runtime.dispatch_all(event)
        await writer
    finally:
        await h.close()
    assert outcomes["code_agent"] == "completed"


async def test_a_redelivered_message_runs_once_and_synthesizes_once(tmp_path, no_sleep):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    await h.store.write(evidence(event, "code_agent"))
    try:
        first, second = await asyncio_gather(h.runtime.dispatch_all(event), h.runtime.dispatch_all(event))
        third = await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    assert first == second == third
    assert h.synthesized == 1
    assert len(h.requests) == 2  # one task per A2A agent, not per delivery


async def test_a_new_attempt_is_a_separate_run(tmp_path, no_sleep):
    first = make_event(attempt=0)
    h = Harness(tmp_path, first, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    retry = first.model_copy(update={"remediation_attempt": 1})
    await h.store.write(evidence(first, "code_agent"))
    try:
        await h.runtime.dispatch_all(first)
        await h.runtime.dispatch_all(retry)
    finally:
        await h.close()
    assert h.synthesized == 2


async def test_required_agents_can_be_narrowed(tmp_path, no_sleep):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    h.runtime.required_agents = ("jenkins_agent",)
    await h.store.write(evidence(event, "jenkins_agent"))
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    assert outcomes == {"jenkins_agent": "completed"}
    assert [str(r.url) for r in h.requests] == [f"{h.runtime.agent_urls['jenkins_agent']}/tasks"]


async def test_without_an_adk_agent_a_watched_agent_is_not_dispatched(tmp_path, no_sleep):
    event = make_event()
    h = Harness(tmp_path, event, {"jenkins_agent": lambda e: ok(e, "jenkins_agent"), "metrics_agent": lambda e: ok(e, "metrics_agent")})
    h.runtime.adk_agent = None
    try:
        outcomes = await h.runtime.dispatch_all(event)
    finally:
        await h.close()
    assert outcomes["code_agent"] == AgentOutcome.NOT_DISPATCHED.value


async def asyncio_gather(*coroutines):
    import asyncio

    return await asyncio.gather(*coroutines)
