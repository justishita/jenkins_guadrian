"""How the Coordinator treats each RabbitMQ message: route it, drop it, or fail it back to the queue.

**Owner:** P2 (Week 6). ``_handle_message`` is the only entry point for incidents, so its three exits must
be right: a valid ``incident.created`` runs ``dispatch_all``; a malformed one is discarded for good (a poison
message must not loop forever); a failure while dispatching propagates inside ``message.process(requeue=True)``
so RabbitMQ redelivers it, and the idempotent ``dispatch_all`` makes that redelivery harmless.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from coordinator.app import CoordinatorRuntime, health

pytestmark = pytest.mark.asyncio


class FakeMessage:
    """The slice of ``aio_pika.IncomingMessage`` the Coordinator uses."""

    def __init__(self, body: dict | bytes, routing_key: str = "incident.created") -> None:
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.routing_key = routing_key
        self.requeue: bool | None = None
        self.error: type[BaseException] | None = None

    def process(self, requeue: bool = False):
        message = self

        class Context:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                message.requeue, message.error = requeue, exc_type
                return False  # never swallow, like aio_pika's process()

        return Context()


def event_body(**overrides) -> dict:
    body = {
        "incident_id": str(uuid4()),
        "job_name": "target-app",
        "build_number": 3,
        "build_url": "http://jenkins:8080/job/target-app/3/",
        "branch": "main",
        "git_commit": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "remediation_attempt": 0,
    }
    body.update(overrides)
    return body


@pytest.fixture
def runtime(monkeypatch):
    runtime = CoordinatorRuntime()
    runtime.calls = []

    async def dispatch_all(event):
        runtime.calls.append(("all", str(event.incident_id)))
        return {}

    async def reinvestigate(body):
        runtime.calls.append(("reinvestigate", body["incident_id"]))
        return {}

    monkeypatch.setattr(runtime, "dispatch_all", dispatch_all)
    monkeypatch.setattr(runtime, "_dispatch_reinvestigation", reinvestigate)
    return runtime


async def test_a_valid_incident_created_message_runs_the_fan_out(runtime):
    body = event_body()
    message = FakeMessage(body)
    await runtime._handle_message(message)
    assert runtime.calls == [("all", body["incident_id"])]
    assert message.requeue is True and message.error is None


async def test_a_malformed_message_is_discarded_without_dispatch_or_error(runtime):
    message = FakeMessage(event_body(build_number="not-a-number"))
    await runtime._handle_message(message)
    assert runtime.calls == [] and message.error is None


async def test_a_message_missing_required_fields_is_discarded(runtime):
    await runtime._handle_message(FakeMessage({"incident_id": str(uuid4())}))
    assert runtime.calls == []


async def test_a_reinvestigation_message_is_routed_to_the_reinvestigation_path(runtime):
    body = {"incident_id": str(uuid4()), "attempt": 1, "previous_failure_summary": "tests still fail", "new_build_url": "http://j/1/"}
    await runtime._handle_message(FakeMessage(body, routing_key="incident.reinvestigate"))
    assert runtime.calls == [("reinvestigate", body["incident_id"])]


async def test_a_message_carrying_a_previous_failure_summary_is_a_reinvestigation_even_on_the_default_key(runtime):
    body = event_body(previous_failure_summary="still red")
    await runtime._handle_message(FakeMessage(body))
    assert runtime.calls == [("reinvestigate", body["incident_id"])]


async def test_a_dispatch_failure_propagates_so_the_message_is_requeued(runtime, monkeypatch):
    async def boom(_event):
        raise RuntimeError("a2a client is down")

    monkeypatch.setattr(runtime, "dispatch_all", boom)
    message = FakeMessage(event_body())
    with pytest.raises(RuntimeError):
        await runtime._handle_message(message)
    assert message.requeue is True and message.error is RuntimeError


# ---- health ---------------------------------------------------------------------------


def request_for(runtime: CoordinatorRuntime):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))


async def test_health_is_unavailable_until_rabbitmq_is_connected():
    with pytest.raises(HTTPException) as caught:
        await health(request_for(CoordinatorRuntime()))
    assert caught.value.status_code == 503


async def test_health_is_unavailable_when_the_connection_closed():
    runtime = CoordinatorRuntime()
    runtime.connection = SimpleNamespace(is_closed=True)
    with pytest.raises(HTTPException):
        await health(request_for(runtime))


async def test_health_is_ok_with_an_open_connection():
    runtime = CoordinatorRuntime()
    runtime.connection = SimpleNamespace(is_closed=False)
    assert await health(request_for(runtime)) == {"status": "ok"}
