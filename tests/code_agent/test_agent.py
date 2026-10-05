"""End-to-end tests for the Code & Remediation agent.

**Owner:** P3. This covers the Week 2 joint deliverable: the agent takes a mock
incident, investigates it on its own, and writes one document to the shared evidence
store. The cases that matter are the unhappy ones - a crash, an unreachable GitHub, a
commit with no changes - because in all of them the Coordinator must still receive an
evidence document saying so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from agents.code_agent.agent import AGENT_NAME, CodeInvestigationAgent, parse_event
from agents.code_agent.tools.github_client import GitHubClient
from common.audit import FileAuditLog
from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent


INCIDENT_ID = UUID("11111111-2222-3333-4444-555555555555")
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

COMMIT_PAYLOAD: dict[str, Any] = {
	"sha": SHA,
	"html_url": f"https://github.com/o/r/commit/{SHA}",
	"commit": {
		"message": "Pin httpx to 0.99.0",
		"author": {"name": "Ishita", "date": "2026-10-05T09:30:00Z"},
	},
	"files": [
		{
			"filename": "target_app/requirements.txt",
			"status": "modified",
			"additions": 1,
			"deletions": 1,
			"patch": "@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0",
		}
	],
}

PULL_REQUEST_PAYLOAD = [
	{
		"number": 42,
		"title": "Upgrade httpx",
		"state": "closed",
		"merged_at": "2026-10-05T09:00:00Z",
		"user": {"login": "ishita"},
		"html_url": "https://github.com/o/r/pull/42",
	}
]


def incident_event(**overrides: Any) -> IncidentCreatedEvent:
	payload: dict[str, Any] = {
		"incident_id": INCIDENT_ID,
		"job_name": "target-app",
		"build_number": 17,
		"build_url": "http://jenkins:8080/job/target-app/17/",
		"branch": "main",
		"git_commit": SHA,
		"failed_stage": "Test",
		"timestamp": datetime(2026, 10, 5, 9, 31, tzinfo=timezone.utc),
	}
	payload.update(overrides)
	return IncidentCreatedEvent.model_validate(payload)


# The three endpoints one investigation touches. Routed by exact path, because
# `/commits/<sha>`, `/commits/<sha>/pulls` and `/commits` are three different calls
# that substring matching would happily conflate.
COMMIT_PATH = f"/repos/o/r/commits/{SHA}"
PULLS_PATH = f"/repos/o/r/commits/{SHA}/pulls"
HISTORY_PATH = "/repos/o/r/commits"

HEALTHY_ROUTES: dict[str, Any] = {
	COMMIT_PATH: COMMIT_PAYLOAD,
	PULLS_PATH: PULL_REQUEST_PAYLOAD,
	HISTORY_PATH: [COMMIT_PAYLOAD],
}


def routed_github(routes: dict[str, Any], *, calls: list[str] | None = None) -> GitHubClient:
	"""A GitHub client whose response is chosen by the exact path requested.

	A route value may be a payload to return or an HTTP status to fail with.
	"""

	def handler(request: httpx.Request) -> httpx.Response:
		path = request.url.path
		if calls is not None:
			calls.append(path)
		payload = routes.get(path, 404)
		if isinstance(payload, int):
			return httpx.Response(payload, text="error")
		return httpx.Response(200, json=payload)

	return GitHubClient(
		"o/r", "ghp_token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
		max_retries=0, backoff_seconds=0,
	)


def build(tmp_path: Path, github: GitHubClient) -> tuple[CodeInvestigationAgent, FileEvidenceStore, FileAuditLog]:
	store = FileEvidenceStore(tmp_path / "evidence")
	audit = FileAuditLog(tmp_path / "audit")
	return CodeInvestigationAgent(github, store, audit), store, audit


@pytest.mark.asyncio
async def test_a_mock_incident_is_investigated_end_to_end(tmp_path: Path) -> None:
	github = routed_github(HEALTHY_ROUTES)
	agent, store, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	stored = await store.read(INCIDENT_ID, AGENT_NAME)
	assert stored is not None
	assert stored.agent == AGENT_NAME
	assert stored.incident_id == INCIDENT_ID
	assert stored.status == "completed"
	assert stored.model_dump(mode="json") == evidence.model_dump(mode="json")


@pytest.mark.asyncio
async def test_the_written_document_satisfies_the_shared_contract(tmp_path: Path) -> None:
	"""It must round-trip through the schema the other two agents also write."""
	github = routed_github(HEALTHY_ROUTES)
	agent, _, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	assert Evidence.model_validate(evidence.model_dump(mode="json")) == evidence
	assert evidence.schema_version == "0.1-stub"
	assert evidence.redaction_applied is True


@pytest.mark.asyncio
async def test_evidence_items_cite_the_commit_the_pr_and_the_changed_file(tmp_path: Path) -> None:
	github = routed_github(HEALTHY_ROUTES)
	agent, _, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())
	ids = {item.id for item in evidence.evidence_items}

	assert "commit-head" in ids
	assert "pr-42" in ids
	assert "file-1" in ids
	assert "dependency-1" in ids
	dependency = next(item for item in evidence.evidence_items if item.id == "dependency-1")
	assert "httpx" in dependency.content


@pytest.mark.asyncio
async def test_every_tool_call_is_recorded_with_its_duration(tmp_path: Path) -> None:
	github = routed_github(HEALTHY_ROUTES)
	agent, _, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	assert {call.tool for call in evidence.tool_calls} == {
		"get_commit",
		"list_pull_requests_for_commit",
		"list_recent_commits",
	}
	assert all(call.duration_ms >= 0 for call in evidence.tool_calls)
	assert all(call.ok for call in evidence.tool_calls)


@pytest.mark.asyncio
async def test_a_moved_dependency_pin_is_classified_as_a_dependency_regression(
	tmp_path: Path,
) -> None:
	"""The fixture commit moves httpx 0.28.1 -> 0.99.0 and the Test stage failed."""
	github = routed_github(HEALTHY_ROUTES)
	agent, _, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	assert evidence.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION
	assert evidence.status == "completed"
	assert evidence.confidence >= 0.5
	assert evidence.root_cause_hypotheses
	assert "httpx" in evidence.root_cause_hypotheses[0].hypothesis


@pytest.mark.asyncio
async def test_every_hypothesis_cites_only_evidence_that_exists(tmp_path: Path) -> None:
	"""The shared contract rejects a dangling citation, so this must hold by construction."""
	github = routed_github(HEALTHY_ROUTES)
	agent, _, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())
	known = {item.id for item in evidence.evidence_items}

	for hypothesis in evidence.root_cause_hypotheses:
		cited = set(hypothesis.supporting_evidence) | set(hypothesis.contradicting_evidence)
		assert cited <= known
		assert hypothesis.supporting_evidence


@pytest.mark.asyncio
async def test_a_commit_that_changed_nothing_reports_insufficient_evidence(tmp_path: Path) -> None:
	empty_commit = {**COMMIT_PAYLOAD, "files": []}
	github = routed_github({**HEALTHY_ROUTES, COMMIT_PATH: empty_commit, PULLS_PATH: []})
	agent, store, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	assert evidence.status == "insufficient_evidence"
	assert evidence.failure_type is FailureTaxonomy.UNKNOWN
	# TC-12: absence of a code change is reported, not explained away.
	assert "does not explain this failure" in evidence.summary
	assert await store.read(INCIDENT_ID, AGENT_NAME) is not None


@pytest.mark.asyncio
async def test_an_unreachable_github_still_produces_an_evidence_document(tmp_path: Path) -> None:
	github = routed_github({path: 503 for path in HEALTHY_ROUTES})
	agent, store, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	assert evidence.status == "failed"
	assert evidence.confidence == 0.0
	assert evidence.root_cause_hypotheses == []
	stored = await store.read(INCIDENT_ID, AGENT_NAME)
	assert stored is not None and stored.status == "failed"


@pytest.mark.asyncio
async def test_partial_retrieval_is_not_treated_as_total_failure(tmp_path: Path) -> None:
	"""A missing commit still leaves the branch history to reason over."""
	calls: list[str] = []
	github = routed_github({**HEALTHY_ROUTES, COMMIT_PATH: 404}, calls=calls)
	agent, _, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	assert evidence.status != "failed"
	assert any(item.id.startswith("history-") for item in evidence.evidence_items)


@pytest.mark.asyncio
async def test_a_crash_is_reported_as_evidence_rather_than_silence(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	github = routed_github(HEALTHY_ROUTES)
	agent, store, _ = build(tmp_path, github)

	async def explode(self: Any, state: Any) -> Any:
		raise RuntimeError("analysis blew up")

	monkeypatch.setattr(CodeInvestigationAgent, "analyze", explode)
	agent, store, _ = build(tmp_path, github)

	evidence = await agent.run_event(incident_event())

	assert evidence.status == "failed"
	assert evidence.confidence == 0.0
	assert "analysis blew up" in evidence.summary
	assert await store.read(INCIDENT_ID, AGENT_NAME) is not None


@pytest.mark.asyncio
async def test_the_run_is_bracketed_in_the_audit_trail(tmp_path: Path) -> None:
	github = routed_github(HEALTHY_ROUTES)
	agent, _, audit = build(tmp_path, github)

	await agent.run_event(incident_event())
	trail = await audit.read(INCIDENT_ID)
	event_types = [entry.event_type for entry in trail]

	assert event_types[0] == "agent_started"
	assert event_types[-1] == "agent_completed"
	assert event_types.count("tool_call") == 3
	assert "evidence_written" in event_types
	assert all(entry.actor == AGENT_NAME for entry in trail)


@pytest.mark.asyncio
async def test_a_failed_tool_call_is_recorded_as_not_ok(tmp_path: Path) -> None:
	github = routed_github({**HEALTHY_ROUTES, COMMIT_PATH: 404})
	agent, _, audit = build(tmp_path, github)

	await agent.run_event(incident_event())
	trail = await audit.read(INCIDENT_ID)

	failed = [entry for entry in trail if entry.event_type == "tool_call" and entry.ok is False]
	assert [entry.summary for entry in failed] == ["get_commit"]


@pytest.mark.asyncio
async def test_a_secret_in_a_commit_message_never_reaches_the_evidence_store(
	tmp_path: Path,
) -> None:
	payload = {
		**COMMIT_PAYLOAD,
		"commit": {
			**COMMIT_PAYLOAD["commit"],
			"message": "hotfix: rotate api_key=sk-live-abcdef123456",
		},
	}
	github = routed_github({**HEALTHY_ROUTES, COMMIT_PATH: payload, PULLS_PATH: []})
	agent, store, _ = build(tmp_path, github)

	await agent.run_event(incident_event())
	stored = await store.read(INCIDENT_ID, AGENT_NAME)

	assert stored is not None
	assert "sk-live-abcdef123456" not in stored.model_dump_json()


@pytest.mark.asyncio
async def test_a_second_investigation_replaces_the_document_and_bumps_its_version(
	tmp_path: Path,
) -> None:
	github = routed_github(HEALTHY_ROUTES)
	agent, store, _ = build(tmp_path, github)

	await agent.run_event(incident_event())
	await agent.run_event(incident_event())

	import json

	document = json.loads(
		(tmp_path / "evidence" / str(INCIDENT_ID) / f"{AGENT_NAME}.json").read_text(encoding="utf-8")
	)
	assert document["version"] == 2
	assert document["agent"] == AGENT_NAME


def test_a_valid_message_body_parses_into_an_event() -> None:
	body = incident_event().model_dump_json().encode()
	assert parse_event(body).incident_id == INCIDENT_ID


def test_a_malformed_message_body_is_rejected() -> None:
	with pytest.raises(ValueError):
		parse_event(b"not json")


def test_a_message_missing_required_fields_is_rejected() -> None:
	with pytest.raises(ValueError):
		parse_event(b'{"incident_id": "11111111-2222-3333-4444-555555555555"}')
