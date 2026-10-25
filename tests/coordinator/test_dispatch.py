import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from common.models import IncidentCreatedEvent
from coordinator.app import CoordinatorRuntime


def make_event() -> IncidentCreatedEvent:
    return IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="target-app",
        build_number=17,
        build_url="http://jenkins:8080/job/target-app/17/",
        branch="main",
        git_commit="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        timestamp=datetime.now(timezone.utc),
        remediation_attempt=2,
    )


@pytest.mark.asyncio
async def test_dispatch_sends_authenticated_a2a_task(monkeypatch):
    event = make_event()
    monkeypatch.setenv("A2A_SHARED_TOKEN", "test-a2a-token")
    runtime = CoordinatorRuntime()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "incident_id": str(event.incident_id),
                "attempt": event.remediation_attempt,
                "blackboard_ref": {
                    "incident_id": str(event.incident_id),
                    "section": "evidence",
                    "key": "jenkins_agent",
                    "version": 1,
                },
            },
        )

    runtime.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {runtime.a2a_token}"},
    )
    try:
        result = await runtime.dispatch_jenkins_task(event)
    finally:
        await runtime.client.aclose()

    assert result["status"] == "completed"
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer test-a2a-token"
    assert requests[0].url.path == "/tasks"
    assert json.loads(requests[0].read()) == {
        "incident_id": str(event.incident_id),
        "attempt": event.remediation_attempt,
    }


@pytest.mark.asyncio
async def test_dispatch_retries_server_errors(monkeypatch):
    event = make_event()
    runtime = CoordinatorRuntime()
    requests = 0
    sleeps: list[float] = []

    async def no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("coordinator.app.asyncio.sleep", no_wait)

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "incident_id": str(event.incident_id),
                "attempt": event.remediation_attempt,
                "blackboard_ref": {},
            },
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await runtime.dispatch_jenkins_task(event)
    finally:
        await runtime.client.aclose()

    assert result["status"] == "completed"
    assert requests == 2
    assert sleeps == [0.25]
