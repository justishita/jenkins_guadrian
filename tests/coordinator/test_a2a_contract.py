"""Tests for the A2A notification contract.

**Owner:** P3. The contract's whole point is that A2A carries *notification*, not
findings, so the blackboard stays the only route by which evidence moves. These tests
pin that down and check that a malformed message is rejected rather than trusted.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from coordinator.a2a_contract import (
	AGENT_KEY,
	AGENT_NAMES,
	EVIDENCE_READY,
	INCIDENT_ID_KEY,
	KIND_KEY,
	A2AContractError,
	agent_card,
	evidence_ready,
	parse_evidence_ready,
)


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")


def test_all_three_investigation_agents_are_named() -> None:
	assert set(AGENT_NAMES) == {"jenkins_agent", "metrics_agent", "code_agent"}


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_a_notification_round_trips(agent: str) -> None:
	incident_id, parsed_agent = parse_evidence_ready(evidence_ready(INCIDENT, agent))

	assert incident_id == INCIDENT
	assert parsed_agent == agent


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_the_notification_carries_no_findings(agent: str) -> None:
	"""A2A announces that evidence exists; it never ships the evidence itself."""
	message = evidence_ready(INCIDENT, agent)
	text = " ".join(part.text for part in message.parts)

	assert str(INCIDENT) in text
	assert agent in text
	for leaked in ("confidence", "failure_type", "hypothesis", "dependency_regression"):
		assert leaked not in text.lower()
	assert set(message.metadata) == {KIND_KEY, INCIDENT_ID_KEY, AGENT_KEY}


def test_each_notification_has_its_own_id_but_shares_the_incident_context() -> None:
	first = evidence_ready(INCIDENT, "code_agent")
	second = evidence_ready(INCIDENT, "metrics_agent")

	assert first.message_id != second.message_id
	assert first.context_id == second.context_id == str(INCIDENT)


def test_a_string_incident_id_is_accepted_and_normalised() -> None:
	incident_id, _ = parse_evidence_ready(evidence_ready(str(INCIDENT), "code_agent"))

	assert incident_id == INCIDENT


def test_an_unknown_agent_is_refused_when_building() -> None:
	with pytest.raises(A2AContractError):
		evidence_ready(INCIDENT, "coordinator")


def test_a_malformed_incident_id_is_refused_when_building() -> None:
	with pytest.raises(ValueError):
		evidence_ready("not-a-uuid", "code_agent")


def test_a_message_of_the_wrong_kind_is_refused() -> None:
	message = evidence_ready(INCIDENT, "code_agent")
	message.metadata[KIND_KEY] = "evidence.deleted"

	with pytest.raises(A2AContractError, match="evidence.ready"):
		parse_evidence_ready(message)


def test_a_message_from_an_unknown_agent_is_refused() -> None:
	message = evidence_ready(INCIDENT, "code_agent")
	message.metadata[AGENT_KEY] = "rogue_agent"

	with pytest.raises(A2AContractError, match="rogue_agent"):
		parse_evidence_ready(message)


def test_a_message_with_a_broken_incident_id_is_refused() -> None:
	message = evidence_ready(INCIDENT, "code_agent")
	message.metadata[INCIDENT_ID_KEY] = "not-a-uuid"
	message.context_id = "not-a-uuid"

	with pytest.raises(A2AContractError, match="invalid incident id"):
		parse_evidence_ready(message)


def test_a_message_with_no_metadata_at_all_is_refused() -> None:
	message = evidence_ready(INCIDENT, "code_agent")
	message.metadata.Clear()  # protobuf map API

	with pytest.raises(A2AContractError):
		parse_evidence_ready(message)


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_every_agent_publishes_a_card_with_one_skill(agent: str) -> None:
	card = agent_card(agent, url="http://localhost:8000/.well-known/agent.json")

	assert card.name == agent
	assert card.description
	assert len(card.skills) == 1
	assert card.skills[0].id == f"{agent}.investigate"
	assert "application/json" in card.default_output_modes


def test_a_card_for_an_unknown_agent_is_refused() -> None:
	with pytest.raises(A2AContractError):
		agent_card("nope", url="")


def test_a_card_without_a_url_is_still_valid() -> None:
	assert agent_card("code_agent", url="").name == "code_agent"


def test_notifications_for_different_incidents_do_not_collide() -> None:
	other = uuid4()
	first, _ = parse_evidence_ready(evidence_ready(INCIDENT, "code_agent"))
	second, _ = parse_evidence_ready(evidence_ready(other, "code_agent"))

	assert first != second
