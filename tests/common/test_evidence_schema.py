"""Contract tests for the shared evidence schema.

**Owner:** P3. ``common/evidence_schema.json`` is what the three agents integrate
against, so these tests guard the two ways it can silently rot: drifting from the
Pydantic model that validates documents at runtime, and drifting from the failure
taxonomy the whole system classifies with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from common.models import Evidence, FailureTaxonomy
from scripts.generate_evidence_schema import SCHEMA_PATH, build_schema, serialize


STUB_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "common" / "evidence_schema_stub.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
	return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stub_schema() -> dict[str, Any]:
	return json.loads(STUB_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_checked_in_schema_matches_the_model() -> None:
	"""The committed file must be exactly what the generator produces."""
	assert SCHEMA_PATH.read_text(encoding="utf-8") == serialize(build_schema())


def test_schema_declares_identity(schema: dict[str, Any]) -> None:
	assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
	assert schema["$id"].endswith("/evidence.json")
	assert schema["title"] == "Evidence"
	assert schema["type"] == "object"


def test_schema_requires_the_fields_every_agent_must_supply(schema: dict[str, Any]) -> None:
	required = set(schema["required"])
	assert {
		"incident_id",
		"created_at",
		"status",
		"failure_type",
		"summary",
		"confidence",
	} <= required


def test_taxonomy_enum_matches_the_shared_python_enum(schema: dict[str, Any]) -> None:
	schema_values = schema["$defs"]["FailureTaxonomy"]["enum"]
	assert schema_values == [member.value for member in FailureTaxonomy]


def test_every_agent_identity_is_representable(schema: dict[str, Any]) -> None:
	assert set(schema["properties"]["agent"]["enum"]) == {
		"jenkins_agent",
		"metrics_agent",
		"code_agent",
	}


def test_wire_version_still_matches_the_stub_the_other_agents_validate_against(
	schema: dict[str, Any], stub_schema: dict[str, Any]
) -> None:
	"""Delivering the canonical schema must not change the wire version mid-flight.

	P1 and P2 already write ``0.1-stub`` documents. Bumping this is a coordinated
	change: update the model, this assertion, and the other agents in one PR.
	"""
	assert schema["properties"]["schema_version"]["const"] == "0.1-stub"
	assert stub_schema["properties"]["schema_version"]["enum"] == ["0.1-stub"]


def test_canonical_schema_is_a_superset_of_the_stub(
	schema: dict[str, Any], stub_schema: dict[str, Any]
) -> None:
	"""Documents written against the stub stay valid against the canonical schema."""
	assert set(stub_schema["properties"]) <= set(schema["properties"])
	assert set(stub_schema["required"]) <= set(schema["required"]) | {
		"schema_version",
		"agent",
		"root_cause_hypotheses",
		"evidence_items",
	}
	assert (
		stub_schema["properties"]["failure_type"]["enum"]
		== schema["$defs"]["FailureTaxonomy"]["enum"]
	)


def test_a_document_from_each_agent_validates(minimal_evidence_payload: dict[str, Any]) -> None:
	for agent in ("jenkins_agent", "metrics_agent", "code_agent"):
		payload = dict(minimal_evidence_payload, agent=agent)
		assert Evidence.model_validate(payload).agent == agent


@pytest.fixture
def minimal_evidence_payload() -> dict[str, Any]:
	return {
		"schema_version": "0.1-stub",
		"incident_id": "11111111-2222-3333-4444-555555555555",
		"created_at": "2026-10-05T09:30:00Z",
		"status": "completed",
		"failure_type": "config_error",
		"summary": "invalid database_url in settings.yaml",
		"confidence": 0.8,
	}
