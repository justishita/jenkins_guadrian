"""Google ADK Coordinator agent: the blackboard reader.

**Owner:** P3. The three investigation agents run on CrewAI and never call each other.
This is the only component allowed to see all three evidence documents, and it is
built on Google ADK so the system uses both required frameworks with a real division
of labour: CrewAI runs the investigations, ADK coordinates them.

**Scope.** This is the transport and collection layer only. It receives A2A
evidence-ready notifications, reads the blackboard, and reports what is there. It does
**not** fuse the evidence, resolve conflicting hypotheses or produce an incident
report - that synthesis is not implemented, and inventing it here would be guessing.
``collect`` therefore returns what each agent concluded, unchanged and unranked.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
import logging
from typing import Any
from uuid import UUID

from google.adk.agents import BaseAgent
from google.genai import types as genai_types

from a2a.types import Message
from common.evidence_store import EvidenceStore
from common.models import Evidence

from coordinator.a2a_contract import AGENT_NAMES, A2AContractError, parse_evidence_ready


logger = logging.getLogger(__name__)

COORDINATOR_NAME = "coordinator"


@dataclass(frozen=True, slots=True)
class BlackboardView:
    """What the blackboard holds for one incident at the moment it was read."""

    incident_id: UUID
    evidence: tuple[Evidence, ...] = ()

    @property
    def reporting_agents(self) -> tuple[str, ...]:
        return tuple(sorted(item.agent for item in self.evidence))

    @property
    def missing_agents(self) -> tuple[str, ...]:
        """Agents that have not written yet. Empty means every agent has reported."""
        return tuple(sorted(set(AGENT_NAMES) - set(self.reporting_agents)))

    @property
    def complete(self) -> bool:
        return not self.missing_agents

    def summary(self) -> str:
        if not self.evidence:
            return f"No agent has written evidence for incident {self.incident_id} yet."
        parts = [
            f"{item.agent}: {item.failure_type.value} at {item.confidence:.2f} ({item.status})"
            for item in sorted(self.evidence, key=lambda e: e.agent)
        ]
        tail = "" if self.complete else f"; still waiting on {', '.join(self.missing_agents)}"
        return f"Incident {self.incident_id} - " + "; ".join(parts) + tail


@dataclass
class _Inbox:
    """Which agents have announced themselves for an incident, in arrival order."""

    seen: dict[UUID, list[str]] = field(default_factory=dict)

    def record(self, incident_id: UUID, agent: str) -> list[str]:
        arrived = self.seen.setdefault(incident_id, [])
        if agent not in arrived:
            arrived.append(agent)
        return arrived


class CoordinatorAgent(BaseAgent):
    """ADK agent that collects the three agents' evidence off the blackboard.

    It holds no opinion about the evidence. Fusion is future work, and until it exists
    this agent's honest output is "here is what each agent independently concluded".
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __init__(self, evidence_store: EvidenceStore, name: str = COORDINATOR_NAME) -> None:
        super().__init__(name=name)
        self._store = evidence_store
        self._inbox = _Inbox()

    async def on_evidence_ready(self, message: Message) -> BlackboardView:
        """Handle one A2A notification and return the blackboard as it now stands.

        A malformed message raises ``A2AContractError`` rather than being skipped: a
        notification that cannot be parsed is a contract break worth surfacing.
        """
        incident_id, agent = parse_evidence_ready(message)
        arrived = self._inbox.record(incident_id, agent)
        logger.info(
            "a2a evidence-ready received",
            extra={"incident_id": str(incident_id), "agent": agent, "arrived": len(arrived)},
        )
        return await self.collect(incident_id)

    async def collect(self, incident_id: UUID | str) -> BlackboardView:
        """Read every agent's evidence for an incident off the shared store."""
        identifier = UUID(str(incident_id))
        evidence = await self._store.read_all(identifier)
        return BlackboardView(incident_id=identifier, evidence=tuple(evidence))

    async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
        """ADK entry point: report the blackboard for the incident in session state.

        The incident id is read from the invocation's session state under
        ``incident_id``; without one there is nothing to collect and the agent says so
        rather than inventing a target.
        """
        from google.adk.events import Event

        state = getattr(getattr(ctx, "session", None), "state", None) or {}
        incident_id = state.get("incident_id")
        if not incident_id:
            text = "No incident_id in session state; nothing to collect."
        else:
            try:
                text = (await self.collect(incident_id)).summary()
            except (ValueError, A2AContractError) as error:
                text = f"Could not read the blackboard: {error}"

        yield Event(
            author=self.name,
            invocation_id=getattr(ctx, "invocation_id", ""),
            content=genai_types.Content(parts=[genai_types.Part(text=text)]),
        )


__all__ = ["COORDINATOR_NAME", "BlackboardView", "CoordinatorAgent"]
