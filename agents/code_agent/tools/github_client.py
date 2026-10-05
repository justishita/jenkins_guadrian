"""Async GitHub client for retrieving the commits and pull requests around a failure.

**Owner:** P3. This is the Code agent's only route to GitHub, and it is read-only for
Weeks 1-4: the agent looks at what changed, it does not change anything. Draft pull
requests arrive in Week 5 behind the policy gate and human approval.

Every response is redacted before it leaves this module, because a commit message or a
diff is one of the easier places for a credential to end up.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import logging
import random
from typing import Any
from urllib.parse import quote

import httpx

from common.redaction import redact


logger = logging.getLogger(__name__)

#: GitHub signals rate limiting with 403 plus a zero remaining-quota header, and with
#: 429. Both are worth waiting out; a plain 403 is a permissions problem and is not.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: A diff large enough to exceed this is not a minimal change and is not worth paying
#: to read in full; the file list still tells us what was touched.
MAX_PATCH_CHARS = 20_000


class GitHubError(Exception):
    """Base exception for GitHub client errors."""


class GitHubAuthError(GitHubError):
    """The configured token was rejected or lacks the required scope."""


class GitHubNotFound(GitHubError):
    """The requested repository, commit or pull request does not exist."""


class GitHubUnavailable(GitHubError):
    """GitHub could not be reached, or kept failing, within the retry budget."""


@dataclass(frozen=True, slots=True)
class CommitSummary:
    """One commit, reduced to what a failure investigation actually uses."""

    sha: str
    message: str
    author: str
    authored_at: str
    url: str

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    @property
    def subject(self) -> str:
        """The first line of the message - what a reviewer scans."""
        return self.message.splitlines()[0] if self.message else ""


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file touched by a commit, pull request or comparison."""

    path: str
    status: str
    additions: int
    deletions: int
    patch: str
    previous_path: str | None = None

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True, slots=True)
class PullRequestSummary:
    """A pull request associated with a commit under investigation."""

    number: int
    title: str
    state: str
    merged: bool
    author: str
    url: str
    body: str = ""


def _redact_text(value: object, *, limit: int | None = None) -> str:
    text = redact(str(value or ""))
    if limit is not None and len(text) > limit:
        return text[:limit] + "\n...[truncated]"
    return text


class GitHubClient:
    """Read-only GitHub REST client with bounded timeouts and retries.

    ``repository`` is ``owner/name``. Each method returns plain dataclasses rather
    than raw JSON so the agent never has to reach into an API response shape.
    """

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        backoff_max: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not repository or repository.count("/") != 1:
            raise ValueError("repository must be in 'owner/name' form")
        if not token:
            raise ValueError("GITHUB_TOKEN must be configured")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        owner, name = repository.split("/")
        self.repository = repository
        self._owner = quote(owner, safe="")
        self._name = quote(name, safe="")
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._backoff_max = backoff_max
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        # Applied after construction so an injected client (tests, shared transport)
        # is authenticated too, rather than quietly making anonymous requests.
        self._client.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- transport ------------------------------------------------------------

    def _repo_path(self, suffix: str) -> str:
        return f"{self._api_url}/repos/{self._owner}/{self._name}{suffix}"

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        return response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"

    def _delay_for(self, attempt: int, response: httpx.Response | None) -> float:
        """Honour GitHub's own backoff hint when it gives one, else exponential."""
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), self._backoff_max)
        # Jitter so three agents retrying the same outage do not sync up.
        base = min(self._backoff_seconds * (2**attempt), self._backoff_max)
        return base * (0.5 + random.random() / 2)

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last_error: str = "no attempt was made"

        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = await self._client.get(url, params=params, timeout=self._timeout)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = f"{type(error).__name__}: {error}"
            else:
                if response.status_code == 404:
                    raise GitHubNotFound(f"{url} does not exist, or the token cannot see it")
                if response.status_code == 401:
                    raise GitHubAuthError("GitHub rejected the configured token")
                if response.status_code == 403 and not self._is_rate_limited(response):
                    raise GitHubAuthError(
                        "the configured token lacks the scope required for this request"
                    )
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as error:
                        raise GitHubUnavailable(f"GitHub returned non-JSON for {url}") from error
                if response.status_code not in _RETRYABLE_STATUS and not self._is_rate_limited(response):
                    raise GitHubError(f"GitHub returned HTTP {response.status_code} for {url}")
                last_error = f"HTTP {response.status_code}"

            if attempt < self._max_retries:
                delay = self._delay_for(attempt, response)
                logger.warning(
                    "github request failed, retrying",
                    extra={"attempt": attempt + 1, "retry_in_seconds": delay, "error": last_error},
                )
                await asyncio.sleep(delay)

        raise GitHubUnavailable(
            f"GitHub unreachable after {self._max_retries + 1} attempts: {redact(last_error)}"
        )

    # --- parsing --------------------------------------------------------------

    @staticmethod
    def _expect_object(payload: Any, url: str) -> dict[str, Any]:
        """Reject an unexpected payload shape as a GitHub error, not a TypeError.

        The agent degrades gracefully around `GitHubError`; an AttributeError from
        deep inside parsing would instead take the whole investigation down.
        """
        if not isinstance(payload, dict):
            raise GitHubUnavailable(
                f"GitHub returned {type(payload).__name__} for {url}, expected an object"
            )
        return payload

    @staticmethod
    def _expect_array(payload: Any, url: str) -> list[Any]:
        if not isinstance(payload, list):
            raise GitHubUnavailable(
                f"GitHub returned {type(payload).__name__} for {url}, expected an array"
            )
        return payload

    @staticmethod
    def _commit_from(payload: dict[str, Any]) -> CommitSummary:
        commit = payload.get("commit") or {}
        author = commit.get("author") or {}
        return CommitSummary(
            sha=str(payload.get("sha", "")),
            message=_redact_text(commit.get("message")),
            author=_redact_text(author.get("name") or (payload.get("author") or {}).get("login")),
            authored_at=str(author.get("date") or ""),
            url=str(payload.get("html_url") or ""),
        )

    @staticmethod
    def _file_from(payload: dict[str, Any]) -> FileChange:
        return FileChange(
            path=str(payload.get("filename", "")),
            status=str(payload.get("status", "modified")),
            additions=int(payload.get("additions") or 0),
            deletions=int(payload.get("deletions") or 0),
            # GitHub omits `patch` for binary and very large files; an empty patch
            # means "we know it changed but cannot show how".
            patch=_redact_text(payload.get("patch"), limit=MAX_PATCH_CHARS),
            previous_path=payload.get("previous_filename"),
        )

    @staticmethod
    def _pull_request_from(payload: dict[str, Any]) -> PullRequestSummary:
        return PullRequestSummary(
            number=int(payload.get("number") or 0),
            title=_redact_text(payload.get("title")),
            state=str(payload.get("state") or ""),
            merged=bool(payload.get("merged_at")),
            author=_redact_text((payload.get("user") or {}).get("login")),
            url=str(payload.get("html_url") or ""),
            body=_redact_text(payload.get("body"), limit=4_000),
        )

    # --- investigation tools --------------------------------------------------

    async def get_commit(self, sha: str) -> tuple[CommitSummary, list[FileChange]]:
        """Return one commit and the files it touched.

        This is the first call of almost every investigation: the build reports the
        commit it failed on, and the question is what that commit did.
        """
        if not sha:
            raise ValueError("sha must not be empty")
        url = self._repo_path(f"/commits/{quote(sha, safe='')}")
        payload = self._expect_object(await self._get(url), url)
        files = [self._file_from(item) for item in payload.get("files") or []]
        return self._commit_from(payload), files

    async def list_recent_commits(self, branch: str, limit: int = 10) -> list[CommitSummary]:
        """Return the most recent commits on a branch, newest first."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        url = self._repo_path("/commits")
        payload = self._expect_array(
            await self._get(url, params={"sha": branch, "per_page": min(limit, 100)}), url
        )
        return [self._commit_from(item) for item in payload if isinstance(item, dict)][:limit]

    async def compare_commits(self, base: str, head: str) -> tuple[list[CommitSummary], list[FileChange]]:
        """Return everything that changed between two commits.

        Used to diff a failing build against the last build that passed, which is the
        narrowest window the real cause can be hiding in.
        """
        if not base or not head:
            raise ValueError("both base and head must be provided")
        url = self._repo_path(f"/compare/{quote(base, safe='')}...{quote(head, safe='')}")
        payload = self._expect_object(await self._get(url), url)
        commits = [self._commit_from(item) for item in payload.get("commits") or []]
        files = [self._file_from(item) for item in payload.get("files") or []]
        return commits, files

    async def list_pull_requests_for_commit(self, sha: str) -> list[PullRequestSummary]:
        """Return the pull requests a commit belongs to.

        The PR description often states intent the commit message leaves out - which
        dependency was upgraded, and why.
        """
        if not sha:
            raise ValueError("sha must not be empty")
        url = self._repo_path(f"/commits/{quote(sha, safe='')}/pulls")
        payload = self._expect_array(await self._get(url), url)
        return [self._pull_request_from(item) for item in payload if isinstance(item, dict)]

    async def get_pull_request_files(self, number: int) -> list[FileChange]:
        """Return the files a pull request changes."""
        if number <= 0:
            raise ValueError("pull request number must be positive")
        url = self._repo_path(f"/pulls/{number}/files")
        payload = self._expect_array(await self._get(url, params={"per_page": 100}), url)
        return [self._file_from(item) for item in payload if isinstance(item, dict)]

    async def get_file_at_ref(self, path: str, ref: str) -> str:
        """Return a file's contents at a ref, for reading a config or manifest as-is.

        Returns an empty string when the file is binary or absent at that ref, which
        is information rather than an error - a deleted config file is a finding.
        """
        if not path:
            raise ValueError("path must not be empty")
        try:
            payload = await self._get(
                self._repo_path(f"/contents/{quote(path, safe='/')}"), params={"ref": ref}
            )
        except GitHubNotFound:
            return ""
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            return ""
        try:
            raw = base64.b64decode(payload.get("content") or "")
            return _redact_text(raw.decode("utf-8"), limit=MAX_PATCH_CHARS)
        except (ValueError, UnicodeDecodeError):
            return ""


__all__ = [
    "MAX_PATCH_CHARS",
    "CommitSummary",
    "FileChange",
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubNotFound",
    "GitHubUnavailable",
    "PullRequestSummary",
]
