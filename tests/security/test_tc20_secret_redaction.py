"""TC-20 Secrets Exposure Test, across every place P3's layers store text.

**Owner:** P3 (secrets redaction, Week 7). The plan's expectation: "sensitive values are
redacted before being sent to the LLM or stored in the audit trail". Each fake secret
below is pushed through every store the investigation writes to - the evidence store,
the blackboard, the audit trail - and through the code agent end to end, with the
secret committed in the diff it reads. None may come out the other side.

The LLM client's own redaction is covered in ``tests/common/test_llm_client.py`` and the
approval comment's in ``tests/test_backend_approvals.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agents.code_agent.agent import CodeInvestigationAgent
from agents.code_agent.tools.github_client import GitHubClient
from common.audit import AuditRecord, FileAuditLog
from common.blackboard import BlackboardEntry, FileBlackboard
from common.evidence_store import FileEvidenceStore
from common.models import Evidence, EvidenceItem, FailureTaxonomy, IncidentCreatedEvent
from common.redaction import redact


# Fake, well-formed credentials of each shape the redactor must recognise.
SECRETS = {
	"github_token": "ghp_FAKEtc20TOKEN0123456789abcdefABCDEF99",
	"bearer_header": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.tc20fakepayload.sig",
	"password_assignment": "DB_PASSWORD=tc20-hunter2-fake",
	"url_credentials": "postgresql://guardian:tc20FakePass@postgres:5432/app",
	"aws_access_key": "AKIATC20FAKEKEY12345",
	"google_api_key": "AIzaTC20fakeGoogleKey0123456789abcdefgh",
	"slack_token": "xoxb-tc20-fake-slack-token",
}

# The part of each that must never survive (the key names may).
SENSITIVE = {
	"github_token": "ghp_FAKEtc20TOKEN0123456789abcdefABCDEF99",
	"bearer_header": "eyJhbGciOiJIUzI1NiJ9.tc20fakepayload.sig",
	"password_assignment": "tc20-hunter2-fake",
	"url_credentials": "tc20FakePass",
	"aws_access_key": "AKIATC20FAKEKEY12345",
	"google_api_key": "AIzaTC20fakeGoogleKey0123456789abcdefgh",
	"slack_token": "xoxb-tc20-fake-slack-token",
}

CASES = pytest.mark.parametrize("kind", sorted(SECRETS))


def everything_under(root: Path) -> str:
	return "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file())


@CASES
def test_the_redactor_masks_each_kind(kind: str) -> None:
	assert SENSITIVE[kind] not in redact(f"build log line: {SECRETS[kind]}")


@CASES
@pytest.mark.asyncio
async def test_the_audit_trail_never_stores_it(kind: str, tmp_path: Path) -> None:
	log = FileAuditLog(tmp_path / "audit")
	await log.record(
		AuditRecord(
			incident_id=uuid4(),
			actor="code_agent",
			event_type="tool_call",
			summary=f"fetched the build log: {SECRETS[kind]}",
			payload={"args": {"header": SECRETS[kind], "nested": [{"line": SECRETS[kind]}]}},
		)
	)
	assert SENSITIVE[kind] not in everything_under(tmp_path)


@CASES
@pytest.mark.asyncio
async def test_the_evidence_store_masks_it_even_when_an_agent_forgot(kind: str, tmp_path: Path) -> None:
	"""``redaction_applied`` is a claim; the store redacts again on write."""
	evidence = Evidence(
		incident_id=uuid4(),
		agent="code_agent",
		created_at=datetime.now(timezone.utc),
		status="completed",
		failure_type=FailureTaxonomy.CONFIG_ERROR,
		summary=f"The config carries {SECRETS[kind]}",
		confidence=0.7,
		evidence_items=[EvidenceItem(id="e1", kind="log_excerpt", source="jenkins", content=SECRETS[kind])],
		redaction_applied=True,
	)
	await FileEvidenceStore(tmp_path / "evidence").write(evidence)
	assert SENSITIVE[kind] not in everything_under(tmp_path)


@CASES
def test_the_blackboard_never_stores_it(kind: str, tmp_path: Path) -> None:
	FileBlackboard(tmp_path / "blackboard").post(
		BlackboardEntry(
			incident_id=uuid4(),
			section="evidence",
			key="code_agent",
			author="code_agent",
			payload={"summary": SECRETS[kind], "items": [{"content": SECRETS[kind]}]},
		)
	)
	assert SENSITIVE[kind] not in everything_under(tmp_path)


@CASES
@pytest.mark.asyncio
async def test_a_secret_committed_in_the_diff_never_leaves_the_code_agent(kind: str, tmp_path: Path) -> None:
	"""End to end: the agent reads a commit containing the secret, as GitHub serves it."""
	sha = "a" * 40
	payload = {
		"sha": sha,
		"html_url": f"https://github.com/o/r/commit/{sha}",
		"commit": {"message": f"Configure the client {SECRETS[kind]}", "author": {"name": "dev", "date": "2026-11-13T09:00:00Z"}},
		"files": [
			{
				"filename": "target_app/config/settings.yaml",
				"status": "modified",
				"additions": 2,
				"deletions": 1,
				"patch": f"@@ -1,1 +1,2 @@\n-timeout_seconds: 5\n+timeout_seconds: 0\n+client: {SECRETS[kind]}",
			}
		],
	}
	routes = {f"/repos/o/r/commits/{sha}": payload, f"/repos/o/r/commits/{sha}/pulls": [], "/repos/o/r/commits": [payload]}
	github = GitHubClient(
		"o/r",
		"ghp_token",
		client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=routes.get(r.url.path, [])))),
		max_retries=0,
	)
	event = IncidentCreatedEvent(
		incident_id=uuid4(),
		job_name="target-app/main",
		build_number=1,
		build_url="http://jenkins:8080/job/target-app/1/",
		branch="main",
		git_commit=sha,
		failed_stage="Test",
		timestamp=datetime(2026, 11, 13, tzinfo=timezone.utc),
	)

	evidence = await CodeInvestigationAgent(
		github, FileEvidenceStore(tmp_path / "evidence"), FileAuditLog(tmp_path / "audit")
	).run_event(event)

	assert evidence.redaction_applied
	assert SENSITIVE[kind] not in evidence.model_dump_json()
	assert SENSITIVE[kind] not in everything_under(tmp_path)
