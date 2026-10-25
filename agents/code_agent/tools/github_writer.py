"""GitHub write access for remediation: branches, file commits, draft pull requests.

**Owner:** P3. Deliberately a separate class from ``GitHubClient``. The investigation
path holds only the read-only client, so no bug in retrieval or reasoning can ever
push a commit - writing requires being handed this object, and only the gated
remediation workflow is.

Writes are **never retried blindly**. A 502 after a POST may mean the branch or pull
request was in fact created; retrying would create a duplicate or fail confusingly.
Only failures that happen before any response arrives are retried, and every write
reports exactly what it did so the audit trail can record it.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import quote

import httpx

from common.redaction import redact


logger = logging.getLogger(__name__)


class GitHubWriteError(Exception):
    """A write was rejected. The message says what GitHub objected to."""


class BranchExists(GitHubWriteError):
    """The remediation branch already exists - a previous attempt was not cleaned up."""


class FileChangedUnderneath(GitHubWriteError):
    """The file's blob changed between reading and writing it."""


@dataclass(frozen=True, slots=True)
class FileAtRef:
    """A file's text and the blob sha GitHub needs to update it."""

    path: str
    content: str
    sha: str


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    number: int
    url: str
    draft: bool


class GitHubWriter:
    """Write operations for a single repository, used only by gated remediation."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        timeout: float = 15.0,
        connect_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not repository or repository.count("/") != 1:
            raise ValueError("repository must be in 'owner/name' form")
        if not token:
            raise ValueError("GITHUB_TOKEN must be configured")
        owner, name = repository.split("/")
        self.repository = repository
        self._base = f"{api_url.rstrip('/')}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        self._timeout = timeout
        self._connect_retries = connect_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._client.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    async def __aenter__(self) -> "GitHubWriter":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send once; retry only when the request never reached GitHub."""
        url = f"{self._base}{path}"
        for attempt in range(self._connect_retries + 1):
            try:
                return await self._client.request(method, url, timeout=self._timeout, **kwargs)
            except httpx.ConnectError as error:
                # Nothing was sent, so nothing can have been created. Safe to retry.
                if attempt == self._connect_retries:
                    raise GitHubWriteError(f"{method} {path}: could not connect") from error
                await asyncio.sleep(0.5 * (2**attempt))
            except (httpx.TimeoutException, httpx.TransportError) as error:
                # The request may have landed. Retrying a write here could duplicate it.
                raise GitHubWriteError(
                    f"{method} {path}: outcome unknown ({type(error).__name__}); not retried"
                ) from error
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return redact(response.text[:200])
        return redact(str(body.get("message", body))[:200]) if isinstance(body, dict) else ""

    async def create_branch(self, branch: str, from_sha: str) -> None:
        """Create ``branch`` at ``from_sha``. Refuses to reuse an existing branch."""
        response = await self._send(
            "POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": from_sha}
        )
        if response.status_code == 422 and "already exists" in self._detail(response).lower():
            raise BranchExists(f"branch {branch} already exists")
        if response.status_code >= 400:
            raise GitHubWriteError(
                f"could not create {branch}: HTTP {response.status_code} {self._detail(response)}"
            )

    async def read_file(self, path: str, ref: str) -> FileAtRef:
        """Read a file's current text and blob sha on a branch."""
        response = await self._send("GET", f"/contents/{quote(path, safe='/')}", params={"ref": ref})
        if response.status_code >= 400:
            raise GitHubWriteError(
                f"could not read {path}@{ref}: HTTP {response.status_code} {self._detail(response)}"
            )
        body = response.json()
        if not isinstance(body, dict) or body.get("encoding") != "base64":
            raise GitHubWriteError(f"{path}@{ref} is not a readable text file")
        text = base64.b64decode(body.get("content") or "").decode("utf-8")
        return FileAtRef(path=path, content=text, sha=str(body["sha"]))

    async def commit_file(
        self, path: str, content: str, *, branch: str, message: str, blob_sha: str
    ) -> str:
        """Commit new content for one file, conditioned on its blob sha.

        Passing the sha read earlier means GitHub refuses the write if anything else
        changed the file in between - the edit was computed against that exact text.
        Returns the new commit sha.
        """
        response = await self._send(
            "PUT",
            f"/contents/{quote(path, safe='/')}",
            json={
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "sha": blob_sha,
                "branch": branch,
            },
        )
        if response.status_code in {409, 422} and "sha" in self._detail(response).lower():
            raise FileChangedUnderneath(f"{path} changed on {branch} after it was read")
        if response.status_code >= 400:
            raise GitHubWriteError(
                f"could not commit {path}: HTTP {response.status_code} {self._detail(response)}"
            )
        return str(response.json()["commit"]["sha"])

    async def open_draft_pull_request(
        self, *, head: str, base: str, title: str, body: str
    ) -> PullRequestRef:
        """Open a pull request that is **always** a draft. There is no non-draft path."""
        response = await self._send(
            "POST",
            "/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        if response.status_code >= 400:
            raise GitHubWriteError(
                f"could not open pull request: HTTP {response.status_code} {self._detail(response)}"
            )
        payload = response.json()
        return PullRequestRef(
            number=int(payload["number"]),
            url=str(payload.get("html_url", "")),
            draft=bool(payload.get("draft", True)),
        )


__all__ = [
    "BranchExists",
    "FileAtRef",
    "FileChangedUnderneath",
    "GitHubWriteError",
    "GitHubWriter",
    "PullRequestRef",
]
