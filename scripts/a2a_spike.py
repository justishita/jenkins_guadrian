"""A2A feasibility spike. Owner: P2. Kept as evidence for docs/a2a_spike_findings.md.

Run: `python scripts/a2a_spike.py`. It starts a real A2A server on a free localhost port, hosts
a minimal Coordinator-like executor, and drives it with the SDK client. It is not production
wiring; the real Coordinator service replaces it in the live A2A milestone.

Questions (slice 0 of the Week 4 plan):
  1. Does the installed a2a-sdk server/client API work, and what does it look like?
  2. Can an agent send a minimal evidence-ready notification to a Coordinator endpoint?
  3. Do the incident id and the evidence reference survive transport?
  4. What happens when one agent never reports (timeout behaviour)?
"""

import asyncio
import inspect
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Message, Part, Role
from a2a.types import a2a_pb2 as pb
from starlette.applications import Starlette

from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy
from common.a2a_contract import (
    AGENT_NAMES,
    A2AContractError,
    evidence_ready,
    parse_evidence_ready,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Inbox:
    """What the Coordinator would track: who has announced, per incident, plus wait-with-timeout."""

    def __init__(self) -> None:
        self.arrivals: dict[UUID, dict[str, float]] = {}
        self.cond = asyncio.Condition()

    async def record(self, incident: UUID, agent: str) -> bool:
        async with self.cond:
            seen = self.arrivals.setdefault(incident, {})
            first = agent not in seen
            seen.setdefault(agent, time.monotonic())
            self.cond.notify_all()
            return first

    async def wait_complete(self, incident: UUID, timeout: float) -> tuple[set[str], bool]:
        """Return (agents seen, timed_out). Complete as soon as all three have announced."""
        async def complete() -> None:
            async with self.cond:
                await self.cond.wait_for(lambda: set(self.arrivals.get(incident, {})) >= set(AGENT_NAMES))

        try:
            await asyncio.wait_for(complete(), timeout)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
        return set(self.arrivals.get(incident, {})), timed_out


class SpikeExecutor(AgentExecutor):
    def __init__(self, inbox: Inbox, store: FileEvidenceStore) -> None:
        self.inbox = inbox
        self.store = store
        self.read_back: dict[tuple[UUID, str], Evidence | None] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        try:
            incident, agent = parse_evidence_ready(context.message)
        except A2AContractError as error:
            text = f"rejected: {error}"
        else:
            await self.inbox.record(incident, agent)
            # The notification carries no findings: read them from the blackboard.
            self.read_back[(incident, agent)] = await self.store.read(incident, agent)
            text = f"ack {agent} {incident}"
        reply = Message(message_id=str(uuid4()), context_id=context.context_id or "", role=Role.ROLE_AGENT)
        reply.parts.append(Part(text=text))
        await event_queue.enqueue_event(reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def coordinator_card(port: int) -> pb.AgentCard:
    card = pb.AgentCard(
        name="coordinator",
        description="Collects evidence-ready notifications and fuses the agents' evidence.",
        version="1.0.0",
        capabilities=pb.AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
    )
    card.supported_interfaces.append(
        pb.AgentInterface(url=f"http://127.0.0.1:{port}/a2a", protocol_binding="JSONRPC", protocol_version="1.0")
    )
    card.skills.append(pb.AgentSkill(id="coordinator.collect", name="collect_evidence", description="x", tags=["ci"]))
    return card


def evidence(incident: UUID, agent: str) -> Evidence:
    return Evidence(
        incident_id=incident, agent=agent, created_at=datetime.now(timezone.utc), status="completed",  # type: ignore[arg-type]
        failure_type=FailureTaxonomy.TIMEOUT, summary=f"{agent} finding", confidence=0.7, redaction_applied=True,
    )


async def send(client, message: Message) -> list[str]:
    texts: list[str] = []
    async for response in client.send_message(pb.SendMessageRequest(message=message)):
        if response.HasField("message"):
            texts.extend(p.text for p in response.message.parts)
    return texts


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


async def main() -> None:
    port = free_port()
    tmp = tempfile.TemporaryDirectory()
    store = FileEvidenceStore(Path(tmp.name))
    inbox = Inbox()
    executor = SpikeExecutor(inbox, store)
    card = coordinator_card(port)
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card)
    app = Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, rpc_url="/a2a")])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    print(f"A2A server up on :{port}")

    print("\n[1] SDK API: client created from the agent-card URL")
    created = create_client(f"http://127.0.0.1:{port}", ClientConfig(streaming=False))
    client = await created if inspect.isawaitable(created) else created
    check("client created via create_client(url)", client is not None, type(client).__name__)

    print("\n[2] minimal notification reaches the Coordinator endpoint")
    incident = uuid4()
    await store.write(evidence(incident, "metrics_agent"))
    t0 = time.perf_counter()
    reply = await send(client, evidence_ready(incident, "metrics_agent"))
    ms = (time.perf_counter() - t0) * 1000
    check("server acknowledged over HTTP/JSON-RPC", bool(reply) and reply[0].startswith("ack"), f"{reply} in {ms:.0f} ms")

    print("\n[3] incident id and evidence reference survive transport")
    seen = inbox.arrivals.get(incident, {})
    check("incident id arrived unchanged", incident in inbox.arrivals, str(incident))
    check("agent name arrived unchanged", "metrics_agent" in seen)
    doc = executor.read_back.get((incident, "metrics_agent"))
    check("the evidence document is retrievable from the blackboard with (incident, agent)", doc is not None and doc.incident_id == incident)

    print("\n[3b] a malformed notification is rejected, not trusted")
    bad = Message(message_id=str(uuid4()), context_id=str(incident), role=Role.ROLE_AGENT)
    bad.parts.append(Part(text="hello"))
    bad.metadata["kind"] = "something.else"
    reply = await send(client, bad)
    check("rejected with a reason", bool(reply) and reply[0].startswith("rejected"), str(reply))

    print("\n[4] timeout when one agent never reports")
    incident2 = uuid4()
    for agent in ("jenkins_agent", "metrics_agent"):
        await send(client, evidence_ready(incident2, agent))
    t0 = time.perf_counter()
    arrived, timed_out = await inbox.wait_complete(incident2, timeout=1.0)
    waited = time.perf_counter() - t0
    missing = set(AGENT_NAMES) - arrived
    check("timed out after ~1s with code_agent missing", timed_out and missing == {"code_agent"}, f"waited {waited:.2f}s, missing={sorted(missing)}")
    await send(client, evidence_ready(incident2, "code_agent"))
    t0 = time.perf_counter()
    arrived, timed_out = await inbox.wait_complete(incident2, timeout=1.0)
    check("completes immediately once the third agent reports", not timed_out and len(arrived) == 3, f"{(time.perf_counter()-t0)*1000:.0f} ms")

    print("\n[5] concurrent + duplicate notifications")
    incident3 = uuid4()
    msgs = [evidence_ready(incident3, a) for a in AGENT_NAMES] + [evidence_ready(incident3, "metrics_agent")]
    await asyncio.gather(*(send(client, m) for m in msgs))
    arrived, timed_out = await inbox.wait_complete(incident3, timeout=1.0)
    check("three concurrent agents + one duplicate recorded once each", not timed_out and arrived == set(AGENT_NAMES))

    server.should_exit = True
    await serve
    tmp.cleanup()


asyncio.run(main())

