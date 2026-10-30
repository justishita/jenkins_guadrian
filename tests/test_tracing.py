"""Tracing is wired end to end: one trace follows an incident from the webhook to the agents.

The chain under test: the webhook starts the root span, the incident's blackboard entry and its
``incident.created`` message carry that trace, the Coordinator continues it, and every A2A call it
makes carries the header the agent's task server extracts.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import common.blackboard as bb_module
from backend.config import settings
from backend.db.database import get_db
from backend.events.publisher import RabbitMQPublisher
from backend.main import app
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import IncidentCreatedEvent
from common.tracing import (
    context_from_headers,
    current_traceparent_headers,
    get_current_trace_and_span_ids,
    setup_tracing,
)
from agents.code_agent import agent as code_agent_module
from agents.metrics_agent import consumer as metrics_consumer
from coordinator.app import CoordinatorRuntime


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    setup_tracing("test")
    memory = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), "setup_tracing must install an SDK provider"
    provider.add_span_processor(SimpleSpanProcessor(memory))
    return memory


@pytest.fixture(autouse=True)
def clean(exporter: InMemorySpanExporter):
    exporter.clear()
    yield
    exporter.clear()


def hex_trace_id(span) -> str:
    return f"{span.context.trace_id:032x}"


# --- the shared module --------------------------------------------------------------


def test_setup_tracing_installs_one_provider_and_is_safe_to_repeat(exporter):
    first = trace.get_tracer_provider()
    setup_tracing("another-service")
    assert trace.get_tracer_provider() is first


def test_headers_carry_the_active_span_and_nothing_without_one(exporter):
    assert current_traceparent_headers() == {}
    with trace.get_tracer("t").start_as_current_span("parent") as parent:
        headers = current_traceparent_headers()
        assert headers["traceparent"].split("-")[1] == f"{parent.get_span_context().trace_id:032x}"


def test_context_from_headers_accepts_bytes_values_and_any_header_case(exporter):
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("parent") as parent:
        headers = current_traceparent_headers()
    amqp_style = {"Traceparent": headers["traceparent"].encode()}
    with tracer.start_as_current_span("child", context=context_from_headers(amqp_style)) as child:
        assert child.get_span_context().trace_id == parent.get_span_context().trace_id


def test_context_from_headers_tolerates_missing_or_garbage_headers(exporter):
    with trace.get_tracer("t").start_as_current_span("orphan", context=context_from_headers(None)):
        pass
    with trace.get_tracer("t").start_as_current_span("orphan2", context=context_from_headers({"traceparent": "nonsense"})):
        pass
    assert {s.name for s in exporter.get_finished_spans()} == {"orphan", "orphan2"}


# --- the blackboard -----------------------------------------------------------------


def test_blackboard_entries_are_stamped_with_the_active_trace(exporter, tmp_path):
    board = FileBlackboard(tmp_path / "bb")
    incident = uuid4()

    with trace.get_tracer("t").start_as_current_span("writer") as span:
        board.post(BlackboardEntry(incident_id=incident, section="context", key="incident_event",
                                   author="backend", payload={"a": 1}))
        trace_id, span_id = get_current_trace_and_span_ids()

    entry = board.read(incident, section="context", key="incident_event")
    assert entry.trace_id == trace_id == f"{span.get_span_context().trace_id:032x}"
    assert entry.span_id == span_id


def test_blackboard_entries_without_a_span_have_no_trace_and_keep_an_explicit_one(exporter, tmp_path):
    board = FileBlackboard(tmp_path / "bb")
    incident = uuid4()
    board.post(BlackboardEntry(incident_id=incident, section="context", key="incident_event",
                               author="backend", payload={"a": 1}))
    assert board.read(incident, section="context", key="incident_event").trace_id is None

    other = uuid4()
    with trace.get_tracer("t").start_as_current_span("writer"):
        board.post(BlackboardEntry(incident_id=other, section="context", key="incident_event", author="backend",
                                   payload={"a": 1}, trace_id="f" * 32, span_id="e" * 16))
    assert board.read(other, section="context", key="incident_event").trace_id == "f" * 32


# --- the publisher ------------------------------------------------------------------


class FakeExchange:
    def __init__(self) -> None:
        self.messages = []

    async def publish(self, message, routing_key):
        self.messages.append((message, routing_key))
        return True


async def test_published_messages_carry_the_trace_as_a_header(exporter):
    publisher = RabbitMQPublisher()
    publisher.exchange = FakeExchange()

    with trace.get_tracer("t").start_as_current_span("producer") as span:
        assert await publisher.publish_event("incident.created", {"incident_id": "x"})
    assert await publisher.publish_event("incident.created", {"incident_id": "y"})

    traced, untraced = (m for m, _ in publisher.exchange.messages)
    assert traced.headers["traceparent"].split("-")[1] == f"{span.get_span_context().trace_id:032x}"
    assert not untraced.headers


# --- the coordinator ----------------------------------------------------------------


class FakeMessage:
    def __init__(self, body: dict, headers: dict | None = None, routing_key: str = "incident.created") -> None:
        self.body = json.dumps(body).encode()
        self.routing_key = routing_key
        self.headers = headers

    def process(self, requeue: bool = False):
        class Context:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return Context()


def incident_body() -> dict:
    return {
        "incident_id": str(uuid4()),
        "job_name": "target-app",
        "build_number": 3,
        "build_url": "http://jenkins:8080/job/target-app/3/",
        "branch": "main",
        "git_commit": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def test_the_coordinator_continues_the_trace_from_the_message_header(exporter, monkeypatch):
    runtime = CoordinatorRuntime()
    seen: dict[str, str | None] = {}

    async def dispatch_all(_event):
        seen["trace_id"], _ = get_current_trace_and_span_ids()
        return {}

    monkeypatch.setattr(runtime, "dispatch_all", dispatch_all)

    with trace.get_tracer("backend").start_as_current_span("producer") as producer:
        headers = current_traceparent_headers()
    await runtime._handle_message(FakeMessage(incident_body(), headers=headers))

    assert seen["trace_id"] == f"{producer.get_span_context().trace_id:032x}"
    handled = [s for s in exporter.get_finished_spans() if s.name == "coordinator.handle_message"]
    assert len(handled) == 1 and hex_trace_id(handled[0]) == seen["trace_id"]


async def test_a_message_without_a_trace_still_gets_handled_in_its_own_trace(exporter, monkeypatch):
    runtime = CoordinatorRuntime()
    monkeypatch.setattr(runtime, "dispatch_all", AsyncMock(return_value={}))

    await runtime._handle_message(FakeMessage(incident_body(), headers=None))

    assert [s.name for s in exporter.get_finished_spans()] == ["coordinator.handle_message"]


async def test_a2a_calls_carry_the_header_the_agent_server_extracts(exporter, monkeypatch):
    monkeypatch.setenv("A2A_SHARED_TOKEN", "test-a2a-token")
    runtime = CoordinatorRuntime()
    event = IncidentCreatedEvent.model_validate(incident_body())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "completed", "incident_id": str(event.incident_id),
                                         "attempt": 0, "blackboard_ref": {"section": "evidence"}})

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       headers={"Authorization": f"Bearer {runtime.a2a_token}"})
    try:
        with trace.get_tracer("t").start_as_current_span("dispatch") as span:
            await runtime.dispatch_jenkins_task(event)
    finally:
        await runtime.client.aclose()

    assert requests[0].headers["Authorization"] == "Bearer test-a2a-token"  # the client's own header survives
    assert requests[0].headers["traceparent"].split("-")[1] == f"{span.get_span_context().trace_id:032x}"
    # and the agent side reads it back into the same trace
    carrier = {name.lower(): value for name, value in requests[0].headers.items()}
    with trace.get_tracer("agent").start_as_current_span("a2a_execute_task", context=context_from_headers(carrier)) as agent_span:
        assert agent_span.get_span_context().trace_id == span.get_span_context().trace_id


# --- the whole chain ----------------------------------------------------------------


def test_one_trace_runs_from_the_webhook_to_the_coordinator(exporter, tmp_path, monkeypatch):
    board = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(bb_module, "_default_blackboard", board)
    incident_id = "11111111-2222-3333-4444-555555555555"
    published: dict[str, dict] = {}

    async def capture(payload):
        # Called inside the webhook's span, like the real publisher.
        published["headers"] = current_traceparent_headers()
        return True

    db = AsyncMock()
    db.add = MagicMock()
    none = MagicMock()
    none.scalar_one_or_none.return_value = None
    db.execute.return_value = none

    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    payload = {
        "job_name": "target-app/main", "build_number": 10,
        "build_url": "http://jenkins:8080/job/target-app/job/main/10/", "branch": "main",
        "git_commit": "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "failed_stage": "Test",
        "timestamp": "2026-10-01T09:30:00Z", "incident_id": incident_id, "remediation_attempt": 0,
    }
    try:
        with patch("backend.incidents.publisher.publish_incident_created", new=capture):
            response = TestClient(app).post(
                "/webhooks/jenkins", json=payload,
                headers={"X-Webhook-Token": settings.WEBHOOK_SHARED_SECRET or "a8f9c2d1e3f4b5a6c7d8e9f0a1b2c3d4"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 202

    root = next(s for s in exporter.get_finished_spans() if s.name == "webhook.create_incident")
    assert root.attributes["incident.id"] == incident_id
    assert root.attributes["incident.created"] is True
    trace_id = hex_trace_id(root)

    # the incident's blackboard context and its outgoing message belong to that trace
    assert board.read(UUID(incident_id), section="context", key="incident_event").trace_id == trace_id
    assert published["headers"]["traceparent"].split("-")[1] == trace_id


# --- the agents that consume RabbitMQ themselves (TRIGGER_MODE=rabbitmq) --------------


@pytest.mark.parametrize(
    "process, span_name",
    [
        (metrics_consumer.process_message, "metrics_agent.handle_message"),
        (code_agent_module._process_message, "code_agent.handle_message"),
    ],
)
async def test_rabbitmq_agents_join_the_incident_trace(exporter, process, span_name):
    seen: dict[str, str | None] = {}

    class Agent:
        async def run_event(self, event):
            seen["trace_id"], _ = get_current_trace_and_span_ids()

    with trace.get_tracer("backend").start_as_current_span("producer") as producer:
        headers = current_traceparent_headers()
    await process(FakeMessage(incident_body(), headers=headers), Agent())

    expected = f"{producer.get_span_context().trace_id:032x}"
    assert seen["trace_id"] == expected
    handled = [s for s in exporter.get_finished_spans() if s.name == span_name]
    assert len(handled) == 1 and hex_trace_id(handled[0]) == expected


@pytest.mark.parametrize("process", [metrics_consumer.process_message, code_agent_module._process_message])
async def test_rabbitmq_agents_survive_a_failed_run_and_a_message_without_a_trace(exporter, process):
    class Agent:
        async def run_event(self, event):
            raise RuntimeError("boom")

    await process(FakeMessage(incident_body(), headers=None), Agent())  # must not raise
    await process(FakeMessage({"nonsense": True}, headers=None), Agent())  # malformed: dropped
