"""Tests for GitHub writes and the remediation branching workflow (Week 5).

**Owner:** P3. Writes are where an automated system can do real damage, so these tests
lean on the refusals: a write whose outcome is unknown is not retried, a file that
changed underneath is not overwritten, and a pull request can only ever be a draft.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from agents.code_agent.remediation import (
	EditNotApplicable,
	FileEdit,
	FixPlan,
	apply_plan_to_branch,
)
from agents.code_agent.tools.github_writer import (
	BranchExists,
	FileChangedUnderneath,
	GitHubWriteError,
	GitHubWriter,
)
from common.models import FailureTaxonomy


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")
BASE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
REQS = "fastapi==0.115.12\nhttpx==0.99.0\nuvicorn==0.34.0\n"


class FakeGitHub:
	"""Just enough of the GitHub REST API to exercise the writer, recording each call."""

	def __init__(self, files: dict[str, str]) -> None:
		self.files = dict(files)
		self.branches: dict[str, str] = {}
		self.calls: list[tuple[str, str, Any]] = []
		self.prs: list[dict[str, Any]] = []
		self.fail: dict[tuple[str, str], httpx.Response] = {}

	def handler(self, request: httpx.Request) -> httpx.Response:
		body = json.loads(request.content) if request.content else None
		path = request.url.path.split("/repos/o/r", 1)[1]
		self.calls.append((request.method, path, body))
		key = (request.method, path.split("?")[0])
		if key in self.fail:
			return self.fail[key]

		if request.method == "POST" and path == "/git/refs":
			name = body["ref"].removeprefix("refs/heads/")
			if name in self.branches:
				return httpx.Response(422, json={"message": "Reference already exists"})
			self.branches[name] = body["sha"]
			return httpx.Response(201, json={"ref": body["ref"]})

		if request.method == "GET" and path.startswith("/contents/"):
			file_path = path.removeprefix("/contents/")
			if file_path not in self.files:
				return httpx.Response(404, json={"message": "Not Found"})
			text = self.files[file_path]
			return httpx.Response(
				200,
				json={
					"encoding": "base64",
					"content": base64.b64encode(text.encode()).decode(),
					"sha": f"blob-{hash(text) & 0xFFFF}",
				},
			)

		if request.method == "PUT" and path.startswith("/contents/"):
			file_path = path.removeprefix("/contents/")
			expected = f"blob-{hash(self.files[file_path]) & 0xFFFF}"
			if body["sha"] != expected:
				return httpx.Response(409, json={"message": "does not match sha"})
			self.files[file_path] = base64.b64decode(body["content"]).decode()
			return httpx.Response(200, json={"commit": {"sha": f"commit-{len(self.calls)}"}})

		if request.method == "POST" and path == "/pulls":
			self.prs.append(body)
			return httpx.Response(
				201, json={"number": 42, "html_url": "https://github.com/o/r/pull/42", "draft": body["draft"]}
			)

		return httpx.Response(404, json={"message": "unhandled"})


def writer_for(fake: FakeGitHub) -> GitHubWriter:
	return GitHubWriter(
		"o/r", "ghp_token", client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
	)


def plan(*edits: FileEdit) -> FixPlan:
	return FixPlan(
		incident_id=INCIDENT,
		failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION,
		confidence=0.9,
		base_ref=BASE,
		branch_name="ai-fix/11111111-2222-3333-4444-555555555555-0",
		commit_message="Pin httpx back",
		rationale="Revert the pin.",
		edits=edits or (FileEdit(path="target_app/requirements.txt", before="httpx==0.99.0", after="httpx==0.28.1"),),
	)


# --- the branching workflow ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_fix_lands_on_a_new_branch_from_the_breaking_commit() -> None:
	fake = FakeGitHub({"target_app/requirements.txt": REQS})

	async with writer_for(fake) as writer:
		result = await apply_plan_to_branch(plan(), writer)

	assert result.branch == "ai-fix/11111111-2222-3333-4444-555555555555-0"
	assert fake.branches[result.branch] == BASE
	assert fake.files["target_app/requirements.txt"] == "fastapi==0.115.12\nhttpx==0.28.1\nuvicorn==0.34.0\n"
	assert len(result.commits) == 1


@pytest.mark.asyncio
async def test_only_the_culprit_line_changes() -> None:
	fake = FakeGitHub({"target_app/requirements.txt": REQS})

	async with writer_for(fake) as writer:
		await apply_plan_to_branch(plan(), writer)

	before, after = REQS.splitlines(), fake.files["target_app/requirements.txt"].splitlines()
	changed = [(b, a) for b, a in zip(before, after) if b != a]
	assert changed == [("httpx==0.99.0", "httpx==0.28.1")]


@pytest.mark.asyncio
async def test_edits_to_one_file_become_one_commit() -> None:
	fake = FakeGitHub({"settings.yaml": "a: -5\nb: 0\n"})
	two = plan(
		FileEdit(path="settings.yaml", before="a: -5", after="a: 3"),
		FileEdit(path="settings.yaml", before="b: 0", after="b: 8"),
	)

	async with writer_for(fake) as writer:
		result = await apply_plan_to_branch(two, writer)

	assert len(result.commits) == 1
	assert fake.files["settings.yaml"] == "a: 3\nb: 8\n"


@pytest.mark.asyncio
async def test_nothing_is_pushed_to_the_base_branch() -> None:
	fake = FakeGitHub({"target_app/requirements.txt": REQS})

	async with writer_for(fake) as writer:
		await apply_plan_to_branch(plan(), writer)

	puts = [body for method, _, body in fake.calls if method == "PUT"]
	assert all(body["branch"].startswith("ai-fix/") for body in puts)


@pytest.mark.asyncio
async def test_a_reused_branch_is_refused_not_overwritten() -> None:
	fake = FakeGitHub({"target_app/requirements.txt": REQS})
	fake.branches["ai-fix/11111111-2222-3333-4444-555555555555-0"] = "old"

	async with writer_for(fake) as writer:
		with pytest.raises(BranchExists):
			await apply_plan_to_branch(plan(), writer)

	assert fake.files["target_app/requirements.txt"] == REQS


@pytest.mark.asyncio
async def test_a_stale_fix_is_refused_before_any_commit() -> None:
	"""The file no longer has the bad line: the incident moved on, so does the fix."""
	fake = FakeGitHub({"target_app/requirements.txt": "httpx==0.30.0\n"})

	async with writer_for(fake) as writer:
		with pytest.raises(EditNotApplicable):
			await apply_plan_to_branch(plan(), writer)

	assert not any(method == "PUT" for method, _, _ in fake.calls)


@pytest.mark.asyncio
async def test_a_file_changed_underneath_is_not_overwritten() -> None:
	fake = FakeGitHub({"target_app/requirements.txt": REQS})
	fake.fail[("PUT", "/contents/target_app/requirements.txt")] = httpx.Response(
		409, json={"message": "target_app/requirements.txt does not match sha"}
	)

	async with writer_for(fake) as writer:
		with pytest.raises(FileChangedUnderneath):
			await apply_plan_to_branch(plan(), writer)


# --- the writer ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pull_request_is_always_a_draft() -> None:
	fake = FakeGitHub({})

	async with writer_for(fake) as writer:
		pr = await writer.open_draft_pull_request(head="ai-fix/x", base="main", title="t", body="b")

	assert fake.prs[0]["draft"] is True
	assert pr.draft is True
	assert pr.number == 42


@pytest.mark.asyncio
async def test_a_write_with_an_unknown_outcome_is_not_retried() -> None:
	"""A timeout after a POST may mean it succeeded; retrying could duplicate it."""
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		raise httpx.ReadTimeout("no response", request=request)

	writer = GitHubWriter("o/r", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
	with pytest.raises(GitHubWriteError, match="not retried"):
		await writer.create_branch("ai-fix/x", BASE)

	assert len(attempts) == 1


@pytest.mark.asyncio
async def test_a_write_that_never_connected_is_retried() -> None:
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		if len(attempts) < 2:
			raise httpx.ConnectError("refused", request=request)
		return httpx.Response(201, json={})

	writer = GitHubWriter(
		"o/r", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), connect_retries=2
	)
	await writer.create_branch("ai-fix/x", BASE)

	assert len(attempts) == 2


@pytest.mark.asyncio
async def test_a_rejected_write_does_not_leak_the_token_in_its_error() -> None:
	def handler(request: httpx.Request) -> httpx.Response:
		return httpx.Response(403, json={"message": "bad credentials token=ghp_abcdef1234567890"})

	writer = GitHubWriter("o/r", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
	with pytest.raises(GitHubWriteError) as error:
		await writer.create_branch("ai-fix/x", BASE)

	assert "ghp_abcdef1234567890" not in str(error.value)


def test_the_writer_refuses_a_malformed_repository() -> None:
	with pytest.raises(ValueError):
		GitHubWriter("https://github.com/o/r", "t")


def test_the_writer_refuses_a_missing_token() -> None:
	with pytest.raises(ValueError):
		GitHubWriter("o/r", "")


def test_the_investigation_client_has_no_write_methods() -> None:
	"""Retrieval holds only the read-only client, so it cannot write by accident."""
	from agents.code_agent.tools.github_client import GitHubClient

	for name in ("create_branch", "commit_file", "open_draft_pull_request", "_send"):
		assert not hasattr(GitHubClient, name), name
