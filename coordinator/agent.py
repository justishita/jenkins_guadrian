"""Google ADK Coordinator agent: the blackboard reader.

**Owner:** P3. The three investigation agents run on CrewAI and never call each other.
This is the only component allowed to see all three evidence documents, and it is
built on Google ADK so the system uses both required frameworks with a real division
of labour: CrewAI runs the investigations, ADK coordinates them.

**Scope.** ``collect`` is the transport and collection layer: it receives A2A
evidence-ready notifications and reads the blackboard, returning what each agent
concluded, unchanged and unranked (``BlackboardView`` has no combined verdict, and a
test keeps it that way). Fusion is a separate, pure step in ``coordinator.fusion``;
``synthesize`` chains collect -> fuse -> report store -> audit and returns an
``IncidentReport`` (added by P2, see coordinator/README.md).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from google.adk.agents import BaseAgent
from google.genai import types as genai_types

from a2a.types import Message
from common.audit import AuditLog, AuditRecord
from common.evidence_store import EvidenceStore
from common.models import Evidence

from coordinator.a2a_contract import AGENT_NAMES, A2AContractError, parse_evidence_ready
from coordinator.report import IncidentReport
from coordinator.report_store import ReportStore

if TYPE_CHECKING:
    from coordinator.fusion import FusionConfig


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

    ``collect`` and ``on_evidence_ready`` hold no opinion about the evidence;
    ``synthesize`` derives one through ``coordinator.fusion`` without altering it.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __init__(
        self,
        evidence_store: EvidenceStore,
        name: str = COORDINATOR_NAME,
        *,
        report_store: ReportStore | None = None,
        audit: AuditLog | None = None,
        fusion_config: FusionConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(name=name)
        self._store = evidence_store
        self._inbox = _Inbox()
        self._report_store = report_store
        self._audit = audit
        self._fusion_config = fusion_config
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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

    async def synthesize(self, incident_id: UUID | str) -> IncidentReport:
        """collect -> fuse -> store -> audit. The view and the evidence stay unchanged.

        Fusion lives in ``coordinator.fusion`` and is a pure function; this method only
        orchestrates it. Storage and audit are best-effort add-ons: the report is
        returned even when neither is configured.
        """
        from coordinator.fusion import fuse  # fusion imports BlackboardView from this module

        started = time.perf_counter()
        view = await self.collect(incident_id)
        report = fuse(view, self._fusion_config, now=self._clock())
        if self._report_store is not None:
            await self._report_store.write(report)
        if self._audit is not None:
            await self._audit.record(
                AuditRecord(
                    incident_id=report.incident_id,
                    actor=self.name,
                    event_type="synthesis_completed",
                    summary=report.narrative,
                    payload={
                        "verdict": report.verdict.value,
                        "confidence": report.confidence,
                        "completeness": report.completeness.value,
                        "conflict_status": report.conflict_status.value,
                        "fusion_method": report.fusion_method.value,
                        "missing_agents": report.missing_agents,
                        "failed_agents": report.failed_agents,
                        "ready_for_policy_review": report.ready_for_policy_review,
                    },
                    duration_ms=(time.perf_counter() - started) * 1000,
                    ok=True,
                )
            )
        return report

    async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
        """ADK entry point: synthesize the report for the incident in session state.

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
                text = (await self.synthesize(incident_id)).narrative
            except (ValueError, A2AContractError) as error:
                text = f"Could not read the blackboard: {error}"

        yield Event(
            author=self.name,
            invocation_id=getattr(ctx, "invocation_id", ""),
            content=genai_types.Content(parts=[genai_types.Part(text=text)]),
        )


__all__ = ["COORDINATOR_NAME", "BlackboardView", "CoordinatorAgent"]
