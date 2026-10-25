"""Shared A2A message contracts and agent cards.

**Owner:** P3. The three investigation agents announce their findings to the
Coordinator over the A2A protocol instead of being called directly. The message says
only *that* an agent finished and which incident it finished - the findings themselves
stay on the blackboard (``common.evidence_store``), which the Coordinator reads.

That split is deliberate and is what keeps the blackboard architecture intact: A2A
carries notification, not payload, so no agent can hand another agent a conclusion.

``a2a.types`` are protobuf messages in a2a-sdk 1.x, so they are built field by field
rather than by keyword construction.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid4

from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Message, Part, Role


#: Metadata keys carried on an evidence-ready notification.
INCIDENT_ID_KEY: Final = "incident_id"
AGENT_KEY: Final = "agent"
KIND_KEY: Final = "kind"

#: The only message kind the Coordinator accepts today.
EVIDENCE_READY: Final = "evidence.ready"

AGENT_NAMES: Final[tuple[str, ...]] = ("jenkins_agent", "metrics_agent", "code_agent")

_SKILL_DESCRIPTIONS: Final[dict[str, str]] = {
    "jenkins_agent": "Investigates build logs, stages and test reports for a failed build.",
    "metrics_agent": "Investigates Prometheus metrics around the failure window.",
    "code_agent": "Investigates whether a recent code change explains the failure.",
}


class A2AContractError(ValueError):
    """A message did not match the evidence-ready contract."""


def agent_card(agent: str, url: str) -> AgentCard:
    """Describe one investigation agent as an A2A agent card.

    The card advertises a single skill, because each agent does exactly one thing:
    investigate one incident from its own evidence domain and publish the result.
    """
    if agent not in AGENT_NAMES:
        raise A2AContractError(f"unknown agent {agent!r}; expected one of {AGENT_NAMES}")

    description = _SKILL_DESCRIPTIONS[agent]
    skill = AgentSkill(
        id=f"{agent}.investigate",
        name="investigate_incident",
        description=description,
        tags=["ci", "incident", "investigation"],
    )
    card = AgentCard(
        name=agent,
        description=description,
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=True),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
    )
    card.skills.append(skill)
    if url:
        card.documentation_url = url
    return card


def evidence_ready(incident_id: UUID | str, agent: str) -> Message:
    """Build the notification an agent sends once its evidence is on the blackboard.

    Deliberately carries no findings. A receiver that wants the evidence reads it from
    the shared store, so the blackboard stays the single source of truth.
    """
    if agent not in AGENT_NAMES:
        raise A2AContractError(f"unknown agent {agent!r}; expected one of {AGENT_NAMES}")
    identifier = str(UUID(str(incident_id)))

    message = Message(message_id=str(uuid4()), context_id=identifier, role=Role.ROLE_AGENT)
    message.parts.append(Part(text=f"{agent} published evidence for incident {identifier}"))
    message.metadata[KIND_KEY] = EVIDENCE_READY
    message.metadata[INCIDENT_ID_KEY] = identifier
    message.metadata[AGENT_KEY] = agent
    return message


def parse_evidence_ready(message: Message) -> tuple[UUID, str]:
    """Return ``(incident_id, agent)`` from a notification, or raise.

    Validated rather than trusted: a message arriving over the wire is data, and a
    malformed one must fail here instead of reaching the blackboard read.
    """
    metadata = dict(message.metadata)
    kind = metadata.get(KIND_KEY)
    if kind != EVIDENCE_READY:
        raise A2AContractError(f"expected {EVIDENCE_READY!r} message, got {kind!r}")

    agent = metadata.get(AGENT_KEY)
    if agent not in AGENT_NAMES:
        raise A2AContractError(f"unknown agent {agent!r}; expected one of {AGENT_NAMES}")

    raw_incident = metadata.get(INCIDENT_ID_KEY) or message.context_id
    try:
        incident_id = UUID(str(raw_incident))
    except (TypeError, ValueError) as error:
        raise A2AContractError(f"invalid incident id {raw_incident!r}") from error

    return incident_id, str(agent)


__all__ = [
    "AGENT_KEY",
    "AGENT_NAMES",
    "EVIDENCE_READY",
    "INCIDENT_ID_KEY",
    "KIND_KEY",
    "A2AContractError",
    "agent_card",
    "evidence_ready",
    "parse_evidence_ready",
]
