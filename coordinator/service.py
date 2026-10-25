"""The Coordinator as a running A2A service.

**Owner:** P2 (live A2A slice). ``python -m coordinator.service`` hosts an A2A endpoint the
three agents notify. For each incident it waits until all three agents have written (read
from the blackboard, not trusted from the messages) or ``COORDINATOR_TIMEOUT_SECONDS``
passes, then runs ``CoordinatorAgent.synthesize``. On timeout the report is built from
whatever is there and is marked incomplete; a late notification from a new agent triggers a
fresh synthesis that replaces it.

Known limits (see docs/a2a_spike_findings.md): pending timers live in memory, so a restart
forgets in-flight incidents until the next notification arrives for them (the evidence
itself is safe on the blackboard); the endpoint has no authentication.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Message, Part, Role
from a2a.types import a2a_pb2 as pb
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from common.audit import build_audit_log
from common.evidence_store import FileEvidenceStore
from coordinator.a2a_contract import A2AContractError, parse_evidence_ready
from coordinator.agent import CoordinatorAgent
from coordinator.report_store import ReportStore

logger = logging.getLogger(__name__)

RPC_PATH = "/a2a"


class IncidentTracker:
    """Decides when an incident is ready to synthesize: all agents present, or timed out."""

    def __init__(self, coordinator: CoordinatorAgent, timeout_seconds: float) -> None:
        self._coordinator = coordinator
        self._timeout = timeout_seconds
        self._timers: dict[UUID, asyncio.Task[None]] = {}
        self._synthesized: dict[UUID, frozenset[str]] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def notified(self, incident_id: UUID, agent: str) -> str:
        """Handle one valid notification; returns a short status for the acknowledgement."""
        view = await self._coordinator.collect(incident_id)
        present = frozenset(view.reporting_agents)
        if view.complete:
            self._cancel_timer(incident_id)
            return await self._synthesize_if_new(incident_id, present, "complete")
        if incident_id in self._synthesized:  # a late agent after a timeout report
            return await self._synthesize_if_new(incident_id, present, "late")
        if incident_id not in self._timers:
            self._timers[incident_id] = asyncio.create_task(self._expire(incident_id))
        return f"waiting for {', '.join(view.missing_agents)}"

    async def _expire(self, incident_id: UUID) -> None:
        await asyncio.sleep(self._timeout)
        self._timers.pop(incident_id, None)
        view = await self._coordinator.collect(incident_id)
        logger.warning(
            "coordinator timeout: synthesizing an incomplete report",
            extra={"incident_id": str(incident_id), "missing": list(view.missing_agents)},
        )
        await self._synthesize_if_new(incident_id, frozenset(view.reporting_agents), "timeout")

    def _cancel_timer(self, incident_id: UUID) -> None:
        timer = self._timers.pop(incident_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _synthesize_if_new(self, incident_id: UUID, present: frozenset[str], why: str) -> str:
        lock = self._locks.setdefault(incident_id, asyncio.Lock())
        async with lock:
            if self._synthesized.get(incident_id) == present:
                return "already synthesized"
            report = await self._coordinator.synthesize(incident_id)
            self._synthesized[incident_id] = present
            logger.info(
                "report written",
                extra={"incident_id": str(incident_id), "why": why, "verdict": report.verdict.value},
            )
            return f"synthesized ({why}): {report.verdict.value}"

    async def close(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()


class CoordinatorExecutor(AgentExecutor):
    """A2A adapter: validate the notification, hand it to the tracker, acknowledge."""

    def __init__(self, tracker: IncidentTracker) -> None:
        self._tracker = tracker

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        try:
            incident_id, agent = parse_evidence_ready(context.message)
        except A2AContractError as error:
            text = f"rejected: {error}"
        else:
            text = f"ack {agent} {incident_id}: {await self._tracker.notified(incident_id, agent)}"
        reply = Message(message_id=str(uuid4()), context_id=context.context_id or "", role=Role.ROLE_AGENT)
        reply.parts.append(Part(text=text))
        await event_queue.enqueue_event(reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def agent_card(public_url: str) -> pb.AgentCard:
    card = pb.AgentCard(
        name="coordinator",
        description="Collects evidence-ready notifications and fuses the agents' evidence into a report.",
        version="1.0.0",
        capabilities=pb.AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
    )
    card.supported_interfaces.append(
        pb.AgentInterface(url=f"{public_url.rstrip('/')}{RPC_PATH}", protocol_binding="JSONRPC", protocol_version="1.0")
    )
    card.skills.append(
        pb.AgentSkill(
            id="coordinator.synthesize",
            name="synthesize_incident",
            description="Fuse the investigation agents' evidence for one incident into a report.",
            tags=["ci", "incident", "fusion"],
        )
    )
    return card


def build_app(coordinator: CoordinatorAgent, public_url: str, timeout_seconds: float) -> Starlette:
    tracker = IncidentTracker(coordinator, timeout_seconds)
    card = agent_card(public_url)
    handler = DefaultRequestHandler(
        agent_executor=CoordinatorExecutor(tracker), task_store=InMemoryTaskStore(), agent_card=card
    )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        await tracker.close()

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/health", health),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url=RPC_PATH),
        ],
    )
    app.state.tracker = tracker
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    port = int(os.environ.get("COORDINATOR_PORT", "8090"))
    coordinator = CoordinatorAgent(
        FileEvidenceStore(Path(os.environ.get("EVIDENCE_DIR", "data/evidence"))),
        report_store=ReportStore(Path(os.environ.get("REPORTS_DIR", "data/reports"))),
        audit=build_audit_log(
            database_url=os.environ.get("DATABASE_URL") or None, root=Path(os.environ.get("AUDIT_DIR", "data/audit"))
        ),
    )
    app = build_app(
        coordinator,
        os.environ.get("COORDINATOR_PUBLIC_URL", f"http://127.0.0.1:{port}"),
        float(os.environ.get("COORDINATOR_TIMEOUT_SECONDS", "120")),
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
