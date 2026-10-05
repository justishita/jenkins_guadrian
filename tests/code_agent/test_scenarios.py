"""Run the code agent against the project's test-case scenarios.

**Owner:** P3. This is the Week 3 deliverable in executable form: each scenario in
``scenarios/`` is driven end to end through the real agent - graph, evidence store and
audit trail - and checked against its declared ground truth.

The scenarios are data rather than hand-written tests so that the expectation and the
documentation cannot diverge, and so a new test case is a YAML file rather than a new
test function. A scenario file with no matching behaviour fails loudly; one that is
malformed fails too, because a silently skipped scenario is worse than none.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml

from agents.code_agent.agent import AGENT_NAME, CodeInvestigationAgent
from agents.code_agent.tools.github_client import GitHubClient
from common.audit import FileAuditLog
from common.evidence_store import FileEvidenceStore
from common.models import Evidence


SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"


def load_scenarios() -> list[dict[str, Any]]:
	files = sorted(SCENARIO_DIR.glob("*.yaml"))
	assert files, f"no scenario files found in {SCENARIO_DIR}"
	scenarios: list[dict[str, Any]] = []
	for path in files:
		document = yaml.safe_load(path.read_text(encoding="utf-8"))
		assert isinstance(document, dict), f"{path.name} is not a mapping"
		document["_file"] = path.name
		scenarios.append(document)
	return scenarios


SCENARIOS = load_scenarios()
CODE_AGENT_SCENARIOS = [s for s in SCENARIOS if s.get("agent") == AGENT_NAME]


def scenario_id(scenario: dict[str, Any]) -> str:
	return f"{scenario['id']}-{scenario['name'].replace(' ', '')}"


def github_for(scenario: dict[str, Any]) -> GitHubClient:
	"""A GitHub client that answers with the commit the scenario describes."""
	incident = scenario["incident"]
	commit = scenario["commit"]
	sha = incident["git_commit"]

	commit_payload = {
		"sha": sha,
		"html_url": f"https://github.com/o/r/commit/{sha}",
		"commit": {
			"message": commit.get("message", ""),
			"author": {"name": commit.get("author", "unknown"), "date": "2026-10-05T09:30:00Z"},
		},
		"files": [
			{
				"filename": file["path"],
				"status": file.get("status", "modified"),
				"additions": file.get("additions", 0),
				"deletions": file.get("deletions", 0),
				"patch": file.get("patch", ""),
			}
			for file in commit.get("files") or []
		],
	}
	routes: dict[str, Any] = {
		f"/repos/o/r/commits/{sha}": commit_payload,
		f"/repos/o/r/commits/{sha}/pulls": [],
		"/repos/o/r/commits": [commit_payload],
	}

	def handler(request: httpx.Request) -> httpx.Response:
		return httpx.Response(200, json=routes.get(request.url.path, []))

	return GitHubClient(
		"o/r",
		"ghp_token",
		client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
		max_retries=0,
		backoff_seconds=0,
	)


async def investigate(scenario: dict[str, Any], tmp_path: Path) -> tuple[Evidence, FileAuditLog]:
	from common.models import IncidentCreatedEvent

	incident = scenario["incident"]
	event = IncidentCreatedEvent.model_validate(
		{
			"incident_id": uuid4(),
			"job_name": "target-app",
			"build_number": 1,
			"build_url": "http://jenkins:8080/job/target-app/1/",
			"branch": incident["branch"],
			"git_commit": incident["git_commit"],
			"failed_stage": incident.get("failed_stage"),
			"timestamp": datetime(2026, 10, 5, 9, 31, tzinfo=timezone.utc),
		}
	)
	audit = FileAuditLog(tmp_path / "audit")
	agent = CodeInvestigationAgent(
		github_for(scenario), FileEvidenceStore(tmp_path / "evidence"), audit
	)
	return await agent.run_event(event), audit


def test_every_scenario_declares_what_it_expects() -> None:
	for scenario in SCENARIOS:
		assert {"id", "name", "owner", "agent", "incident", "expected"} <= set(scenario), (
			f"{scenario['_file']} is missing required keys"
		)


def test_the_two_week_three_scenarios_are_present() -> None:
	"""TC-03 and TC-08 are this week's named deliverable."""
	assert {"TC-03", "TC-08"} <= {scenario["id"] for scenario in CODE_AGENT_SCENARIOS}


@pytest.mark.parametrize("scenario", CODE_AGENT_SCENARIOS, ids=scenario_id)
@pytest.mark.asyncio
async def test_scenario_is_classified_as_its_ground_truth_says(
	scenario: dict[str, Any], tmp_path: Path
) -> None:
	expected = scenario["expected"]
	evidence, _ = await investigate(scenario, tmp_path)

	assert evidence.status == expected["status"], evidence.summary
	assert evidence.failure_type.value == expected["failure_type"], evidence.summary

	if "min_confidence" in expected:
		assert evidence.confidence >= expected["min_confidence"], evidence.summary
	if "max_confidence" in expected:
		assert evidence.confidence <= expected["max_confidence"], evidence.summary


@pytest.mark.parametrize("scenario", CODE_AGENT_SCENARIOS, ids=scenario_id)
@pytest.mark.asyncio
async def test_scenario_hypothesis_says_what_it_should(
	scenario: dict[str, Any], tmp_path: Path
) -> None:
	expected = scenario["expected"]
	evidence, _ = await investigate(scenario, tmp_path)
	text = evidence.summary.lower()
	if evidence.root_cause_hypotheses:
		text += " " + " ".join(h.hypothesis.lower() for h in evidence.root_cause_hypotheses)

	for fragment in expected.get("hypothesis_mentions") or []:
		assert str(fragment).lower() in text, f"{fragment!r} missing from: {text}"

	for fragment in expected.get("must_not_mention") or []:
		assert str(fragment).lower() not in text, f"{fragment!r} should not be suggested"

	steps = " ".join(evidence.recommended_next_steps).lower()
	for fragment in expected.get("next_steps_mention") or []:
		assert str(fragment).lower() in steps, f"{fragment!r} missing from next steps: {steps}"


@pytest.mark.parametrize("scenario", CODE_AGENT_SCENARIOS, ids=scenario_id)
@pytest.mark.asyncio
async def test_scenario_cites_the_evidence_its_conclusion_rests_on(
	scenario: dict[str, Any], tmp_path: Path
) -> None:
	expected = scenario["expected"]
	evidence, _ = await investigate(scenario, tmp_path)

	known = {item.id for item in evidence.evidence_items}
	for hypothesis in evidence.root_cause_hypotheses:
		cited = set(hypothesis.supporting_evidence) | set(hypothesis.contradicting_evidence)
		assert cited <= known, f"{scenario['id']} cites evidence that does not exist"

	for kind in expected.get("must_cite_kinds") or []:
		top = evidence.root_cause_hypotheses[0]
		assert any(
			identifier.startswith(f"{kind}-") for identifier in top.supporting_evidence
		), f"{scenario['id']} must cite a {kind} item, got {top.supporting_evidence}"


@pytest.mark.parametrize("scenario", CODE_AGENT_SCENARIOS, ids=scenario_id)
@pytest.mark.asyncio
async def test_scenario_findings_reach_the_audit_layer(
	scenario: dict[str, Any], tmp_path: Path
) -> None:
	"""The Week 3 joint deliverable: each agent logs its findings to the audit layer."""
	evidence, audit = await investigate(scenario, tmp_path)
	trail = await audit.read(evidence.incident_id)
	by_type = {entry.event_type: entry for entry in trail}

	assert "agent_started" in by_type
	assert "hypothesis_formed" in by_type
	assert "evidence_written" in by_type
	assert "agent_completed" in by_type

	formed = by_type["hypothesis_formed"]
	assert formed.payload["failure_type"] == evidence.failure_type.value
	assert formed.payload["confidence"] == pytest.approx(evidence.confidence)
	assert len(formed.payload["hypotheses"]) == len(evidence.root_cause_hypotheses)

	written = by_type["evidence_written"]
	assert written.payload["status"] == evidence.status
	assert written.ok is True


@pytest.mark.parametrize("scenario", CODE_AGENT_SCENARIOS, ids=scenario_id)
@pytest.mark.asyncio
async def test_scenario_evidence_satisfies_the_shared_contract(
	scenario: dict[str, Any], tmp_path: Path
) -> None:
	evidence, _ = await investigate(scenario, tmp_path)

	assert Evidence.model_validate(evidence.model_dump(mode="json")) == evidence
	assert evidence.agent == AGENT_NAME
	assert evidence.redaction_applied is True
