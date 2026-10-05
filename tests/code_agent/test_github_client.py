"""Tests for the Code agent's GitHub client.

**Owner:** P3. The client is the agent's only window onto the repository, so what
matters is that it reports failures as distinguishable errors rather than as empty
results, that it retries only what is worth retrying, and that nothing leaves it
unredacted.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agents.code_agent.tools.github_client import (
	MAX_PATCH_CHARS,
	GitHubAuthError,
	GitHubClient,
	GitHubError,
	GitHubNotFound,
	GitHubUnavailable,
)


REPOSITORY = "justishita/jenkins_guadrian"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

COMMIT_PAYLOAD: dict[str, Any] = {
	"sha": SHA,
	"html_url": f"https://github.com/{REPOSITORY}/commit/{SHA}",
	"commit": {
		"message": "Pin httpx to 0.99.0\n\nSpeculative upgrade.",
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


def client_for(handler: Any, **kwargs: Any) -> GitHubClient:
	transport = httpx.MockTransport(handler)
	return GitHubClient(
		REPOSITORY,
		"ghp_token",
		client=httpx.AsyncClient(transport=transport),
		backoff_seconds=0,
		**kwargs,
	)


def responding(payload: Any, *, status_code: int = 200, headers: dict[str, str] | None = None) -> Any:
	def handler(request: httpx.Request) -> httpx.Response:
		if status_code >= 400:
			return httpx.Response(status_code, text="error", headers=headers or {})
		return httpx.Response(status_code, json=payload)

	return handler


@pytest.mark.asyncio
async def test_get_commit_returns_the_commit_and_its_files() -> None:
	async with client_for(responding(COMMIT_PAYLOAD)) as github:
		commit, files = await github.get_commit(SHA)

	assert commit.short_sha == SHA[:7]
	assert commit.subject == "Pin httpx to 0.99.0"
	assert commit.author == "Ishita"
	assert len(files) == 1
	assert files[0].path == "target_app/requirements.txt"
	assert files[0].changed_lines == 2


@pytest.mark.asyncio
async def test_a_commit_message_containing_a_secret_is_redacted() -> None:
	payload = {
		**COMMIT_PAYLOAD,
		"commit": {
			**COMMIT_PAYLOAD["commit"],
			"message": "fix deploy, api_key=sk-live-abcdef123456",
		},
	}
	async with client_for(responding(payload)) as github:
		commit, _ = await github.get_commit(SHA)

	assert "sk-live-abcdef123456" not in commit.message
	assert "[REDACTED]" in commit.message


@pytest.mark.asyncio
async def test_a_patch_containing_a_secret_is_redacted() -> None:
	payload = {
		**COMMIT_PAYLOAD,
		"files": [
			{
				"filename": "target_app/config/deploy.env",
				"status": "modified",
				"additions": 1,
				"deletions": 0,
				"patch": "@@ -1,0 +1,1 @@\n+GITHUB_TOKEN=ghp_abcdef1234567890",
			}
		],
	}
	async with client_for(responding(payload)) as github:
		_, files = await github.get_commit(SHA)

	assert "ghp_abcdef1234567890" not in files[0].patch


@pytest.mark.asyncio
async def test_an_oversized_patch_is_truncated_not_dropped() -> None:
	payload = {
		**COMMIT_PAYLOAD,
		"files": [
			{
				"filename": "target_app/app/main.py",
				"status": "modified",
				"additions": 5000,
				"deletions": 0,
				"patch": "+line\n" * 40_000,
			}
		],
	}
	async with client_for(responding(payload)) as github:
		_, files = await github.get_commit(SHA)

	assert files[0].patch.endswith("...[truncated]")
	assert len(files[0].patch) < MAX_PATCH_CHARS + 100
	assert files[0].additions == 5000  # the counts survive truncation


@pytest.mark.asyncio
async def test_a_binary_file_has_no_patch_but_is_still_reported() -> None:
	payload = {
		**COMMIT_PAYLOAD,
		"files": [{"filename": "docs/diagram.png", "status": "modified", "additions": 0, "deletions": 0}],
	}
	async with client_for(responding(payload)) as github:
		_, files = await github.get_commit(SHA)

	assert files[0].path == "docs/diagram.png"
	assert files[0].patch == ""


@pytest.mark.asyncio
async def test_a_missing_commit_raises_not_found() -> None:
	async with client_for(responding(None, status_code=404)) as github:
		with pytest.raises(GitHubNotFound):
			await github.get_commit(SHA)


@pytest.mark.asyncio
async def test_a_rejected_token_raises_an_auth_error() -> None:
	async with client_for(responding(None, status_code=401)) as github:
		with pytest.raises(GitHubAuthError):
			await github.get_commit(SHA)


@pytest.mark.asyncio
async def test_a_scope_problem_is_an_auth_error_not_a_retry() -> None:
	"""A plain 403 is a permissions problem; retrying it just wastes the budget."""
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		return httpx.Response(403, text="forbidden", headers={"x-ratelimit-remaining": "4999"})

	async with client_for(handler, max_retries=3) as github:
		with pytest.raises(GitHubAuthError):
			await github.get_commit(SHA)

	assert len(attempts) == 1


@pytest.mark.asyncio
async def test_rate_limiting_is_retried() -> None:
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		if len(attempts) == 1:
			return httpx.Response(403, text="rate limited", headers={"x-ratelimit-remaining": "0"})
		return httpx.Response(200, json=COMMIT_PAYLOAD)

	async with client_for(handler, max_retries=2) as github:
		commit, _ = await github.get_commit(SHA)

	assert commit.sha == SHA
	assert len(attempts) == 2


@pytest.mark.asyncio
async def test_server_errors_are_retried_then_give_up() -> None:
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		return httpx.Response(503, text="unavailable")

	async with client_for(handler, max_retries=2) as github:
		with pytest.raises(GitHubUnavailable):
			await github.get_commit(SHA)

	assert len(attempts) == 3


@pytest.mark.asyncio
async def test_a_transport_error_is_retried_then_reported_as_unavailable() -> None:
	attempts: list[int] = []

	def handler(request: httpx.Request) -> httpx.Response:
		attempts.append(1)
		raise httpx.ConnectError("no route to host", request=request)

	async with client_for(handler, max_retries=1) as github:
		with pytest.raises(GitHubUnavailable):
			await github.get_commit(SHA)

	assert len(attempts) == 2


@pytest.mark.asyncio
async def test_an_unexpected_client_error_is_not_retried() -> None:
	async with client_for(responding(None, status_code=422), max_retries=3) as github:
		with pytest.raises(GitHubError):
			await github.get_commit(SHA)


@pytest.mark.asyncio
async def test_compare_commits_returns_both_commits_and_files() -> None:
	payload = {"commits": [COMMIT_PAYLOAD], "files": COMMIT_PAYLOAD["files"]}
	async with client_for(responding(payload)) as github:
		commits, files = await github.compare_commits("main", SHA)

	assert [commit.sha for commit in commits] == [SHA]
	assert [file.path for file in files] == ["target_app/requirements.txt"]


@pytest.mark.asyncio
async def test_pull_requests_for_a_commit_are_summarised() -> None:
	payload = [
		{
			"number": 42,
			"title": "Upgrade httpx",
			"state": "closed",
			"merged_at": "2026-10-05T09:00:00Z",
			"user": {"login": "ishita"},
			"html_url": "https://github.com/x/y/pull/42",
			"body": "Routine bump.",
		}
	]
	async with client_for(responding(payload)) as github:
		pull_requests = await github.list_pull_requests_for_commit(SHA)

	assert pull_requests[0].number == 42
	assert pull_requests[0].merged is True
	assert pull_requests[0].author == "ishita"


@pytest.mark.asyncio
async def test_recent_commits_are_capped_at_the_requested_limit() -> None:
	async with client_for(responding([COMMIT_PAYLOAD] * 10)) as github:
		commits = await github.list_recent_commits("main", limit=3)

	assert len(commits) == 3


@pytest.mark.asyncio
async def test_a_file_absent_at_a_ref_reads_as_empty_rather_than_raising() -> None:
	"""A deleted config file is a finding, not a transport failure."""
	async with client_for(responding(None, status_code=404)) as github:
		assert await github.get_file_at_ref("target_app/config/settings.yaml", SHA) == ""


@pytest.mark.asyncio
async def test_file_contents_are_decoded_and_redacted() -> None:
	import base64

	content = base64.b64encode(b"database_url: null\ntoken=ghp_abcdef1234567890\n").decode()
	payload = {"encoding": "base64", "content": content}
	async with client_for(responding(payload)) as github:
		text = await github.get_file_at_ref("target_app/config/settings.yaml", SHA)

	assert "database_url: null" in text
	assert "ghp_abcdef1234567890" not in text


@pytest.mark.asyncio
async def test_the_request_targets_the_configured_repository() -> None:
	seen: dict[str, str] = {}

	def handler(request: httpx.Request) -> httpx.Response:
		seen["url"] = str(request.url)
		seen["auth"] = request.headers.get("authorization", "")
		return httpx.Response(200, json=COMMIT_PAYLOAD)

	async with client_for(handler) as github:
		await github.get_commit(SHA)

	assert seen["url"] == f"https://api.github.com/repos/{REPOSITORY}/commits/{SHA}"
	assert seen["auth"] == "Bearer ghp_token"


def test_a_malformed_repository_is_a_configuration_error() -> None:
	with pytest.raises(ValueError):
		GitHubClient("https://github.com/owner/name", "ghp_token")


def test_a_missing_token_is_a_configuration_error() -> None:
	with pytest.raises(ValueError):
		GitHubClient(REPOSITORY, "")


@pytest.mark.asyncio
async def test_empty_arguments_are_rejected_before_a_request_is_made() -> None:
	def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
		raise AssertionError("no request should be made")

	async with client_for(handler) as github:
		with pytest.raises(ValueError):
			await github.get_commit("")
		with pytest.raises(ValueError):
			await github.compare_commits("main", "")
		with pytest.raises(ValueError):
			await github.get_pull_request_files(0)


@pytest.mark.asyncio
async def test_an_unexpected_payload_shape_is_a_github_error_not_a_crash() -> None:
	"""The agent degrades around GitHubError; a TypeError would take it down instead."""
	async with client_for(responding(["not", "an", "object"])) as github:
		with pytest.raises(GitHubUnavailable):
			await github.get_commit(SHA)


@pytest.mark.asyncio
async def test_an_object_where_a_list_belongs_is_a_github_error() -> None:
	async with client_for(responding({"message": "not a list"})) as github:
		with pytest.raises(GitHubUnavailable):
			await github.list_recent_commits("main")


@pytest.mark.asyncio
async def test_non_object_entries_in_a_list_are_skipped() -> None:
	async with client_for(responding([COMMIT_PAYLOAD, "junk", None])) as github:
		commits = await github.list_recent_commits("main")

	assert [commit.sha for commit in commits] == [SHA]
