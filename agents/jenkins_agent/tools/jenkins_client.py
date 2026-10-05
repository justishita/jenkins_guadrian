"""Async HTTP client for Jenkins build investigation endpoints."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import logging
import os
import random
import re
import time
from typing import Any
from urllib.parse import quote

import httpx


logger = logging.getLogger(__name__)
ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[@-_])"
)
CARRIAGE_RETURN_PROGRESS_RE = re.compile(r"[^\n]*\r")


class JenkinsError(Exception):
    """Base exception for Jenkins client errors."""


class JenkinsAuthError(JenkinsError):
    """Raised when Jenkins rejects the configured credentials."""


class JenkinsNotFound(JenkinsError):
    """Raised when a requested Jenkins resource does not exist."""


class JenkinsUnavailable(JenkinsError):
    """Raised when Jenkins cannot be reached or keeps returning server errors."""


JobPath = str | Sequence[str]


@dataclass(frozen=True, slots=True)
class ConsoleText:
    text: str
    truncated: bool
    partial: bool


class JenkinsClient:
    """Async Jenkins API client with bounded transport and server-error retries.

    A string job path is ``multibranch-job/branch``; any slashes after the first
    belong to the branch. Pass a sequence for explicit folder/job components,
    such as ``("folder", "multibranch-job", "feature/sub-branch")``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        api_token: str | None = None,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        backoff_max: float = 2.0,
        build_wait_timeout: float = 30.0,
        build_poll_interval: float = 1.0,
    ) -> None:
        configured_url = base_url or os.getenv("JENKINS_URL")
        configured_user = username or os.getenv("JENKINS_USER")
        configured_token = api_token or os.getenv("JENKINS_API_TOKEN")
        if not configured_url:
            raise ValueError("JENKINS_URL must be configured")
        if not configured_user:
            raise ValueError("JENKINS_USER must be configured")
        if not configured_token:
            raise ValueError("JENKINS_API_TOKEN must be configured")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_base < 0 or backoff_max < 0:
            raise ValueError("backoff delays must be non-negative")
        if build_wait_timeout < 0 or build_poll_interval <= 0:
            raise ValueError("build wait timeout must be non-negative and poll interval positive")

        self.base_url = configured_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.build_wait_timeout = build_wait_timeout
        self.build_poll_interval = build_poll_interval
        self._client = httpx.AsyncClient(
            auth=(configured_user, configured_token),
            timeout=timeout,
        )

    async def __aenter__(self) -> "JenkinsClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _job_path(job: JobPath) -> str:
        if isinstance(job, str):
            normalized = job.strip("/")
            if not normalized:
                raise ValueError("job must not be empty")
            components = normalized.split("/", 1)
        else:
            components = list(job)
            if not components or any(not component for component in components):
                raise ValueError("job path components must not be empty")

        encoded = []
        for component in components:
            if not isinstance(component, str):
                raise TypeError("job path components must be strings")
            encoded_component = quote(component, safe="").replace("%2F", "%252F")
            encoded.append(f"/job/{encoded_component}")
        return "".join(encoded)

    def _build_url(self, job: JobPath, number: int) -> str:
        if number < 0:
            raise ValueError("build number must be non-negative")
        return f"{self.base_url}{self._job_path(job)}/{number}"

    async def _wait_before_retry(self, attempt: int) -> None:
        delay_limit = min(self.backoff_max, self.backoff_base * (2**attempt))
        await asyncio.sleep(random.uniform(0.0, delay_limit))

    @staticmethod
    def _raise_for_client_error(response: httpx.Response) -> None:
        if response.status_code == 401:
            logger.error("Jenkins authentication failed; evidence collection cannot continue")
            raise JenkinsAuthError("Jenkins rejected the configured credentials")
        if response.status_code == 404:
            raise JenkinsNotFound("The requested Jenkins resource was not found")
        response.raise_for_status()

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        not_found_is_none: bool = False,
    ) -> httpx.Response | None:
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(url, params=params)
            except httpx.TransportError as error:
                if attempt == self.max_retries:
                    raise JenkinsUnavailable("Jenkins request failed after retries") from error
                await self._wait_before_retry(attempt)
                continue

            if response.status_code >= 500:
                if attempt == self.max_retries:
                    raise JenkinsUnavailable(
                        f"Jenkins returned HTTP {response.status_code} after retries"
                    )
                await self._wait_before_retry(attempt)
                continue
            if response.status_code == 404 and not_found_is_none:
                return None
            self._raise_for_client_error(response)
            return response

        raise JenkinsUnavailable("Jenkins request failed after retries")

    async def _fetch_build(self, job: JobPath, n: int) -> dict[str, Any]:
        response = await self._request(f"{self._build_url(job, n)}/api/json")
        assert response is not None
        return response.json()

    async def _wait_for_build_completion(self, job: JobPath, n: int) -> tuple[dict[str, Any], bool]:
        deadline = time.monotonic() + self.build_wait_timeout
        build = await self._fetch_build(job, n)
        while build.get("building") is True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.build_poll_interval, remaining))
            if time.monotonic() >= deadline:
                break
            build = await self._fetch_build(job, n)
        return build, build.get("building") is True

    async def get_build(self, job: JobPath, n: int) -> dict[str, Any]:
        build, _ = await self._wait_for_build_completion(job, n)
        return build

    async def get_console_text(
        self,
        job: JobPath,
        n: int,
        max_bytes: int = 2_000_000,
    ) -> ConsoleText:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")

        url = f"{self._build_url(job, n)}/consoleText"
        prefix_limit = max_bytes // 5
        suffix_limit = max_bytes - prefix_limit

        for attempt in range(self.max_retries + 1):
            total_bytes = 0
            captured = bytearray()
            prefix = bytearray()
            suffix = bytearray()
            truncated = False

            try:
                _, partial = await self._wait_for_build_completion(job, n)
                async with self._client.stream("GET", url) as response:
                    if response.status_code >= 500:
                        if attempt == self.max_retries:
                            raise JenkinsUnavailable(
                                f"Jenkins returned HTTP {response.status_code} after retries"
                            )
                        await self._wait_before_retry(attempt)
                        continue
                    self._raise_for_client_error(response)

                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        total_bytes += len(chunk)
                        if not truncated:
                            captured.extend(chunk)
                            if total_bytes > max_bytes:
                                truncated = True
                                prefix.extend(captured[:prefix_limit])
                                suffix.extend(captured[prefix_limit:])
                                if len(suffix) > suffix_limit:
                                    del suffix[: len(suffix) - suffix_limit]
                                captured.clear()
                        elif suffix_limit:
                            suffix.extend(chunk)
                            if len(suffix) > suffix_limit:
                                del suffix[: len(suffix) - suffix_limit]

                if not truncated:
                    text = captured.decode("utf-8", errors="replace")
                    return ConsoleText(self._clean_console_text(text), False, partial)

                omitted_bytes = total_bytes - max_bytes
                marker = f"[... truncated {omitted_bytes} bytes ...]".encode("ascii")
                text = bytes(prefix) + marker + bytes(suffix)
                decoded = text.decode("utf-8", errors="replace")
                return ConsoleText(self._clean_console_text(decoded), True, partial)
            except httpx.TransportError as error:
                if attempt == self.max_retries:
                    raise JenkinsUnavailable("Jenkins console request failed after retries") from error
                await self._wait_before_retry(attempt)

        raise JenkinsUnavailable("Jenkins console request failed after retries")

    @staticmethod
    def _clean_console_text(text: str) -> str:
        text = ANSI_ESCAPE_RE.sub("", text)
        return CARRIAGE_RETURN_PROGRESS_RE.sub("", text)

    async def get_stage_summary(self, job: JobPath, n: int) -> dict[str, Any]:
        response = await self._request(f"{self._build_url(job, n)}/wfapi/describe")
        assert response is not None
        return response.json()

    async def get_test_report(self, job: JobPath, n: int) -> dict[str, Any] | None:
        response = await self._request(
            f"{self._build_url(job, n)}/testReport/api/json",
            not_found_is_none=True,
        )
        return None if response is None else response.json()

    async def get_build_history(
        self,
        job: JobPath,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        response = await self._request(
            f"{self.base_url}{self._job_path(job)}/api/json",
            params={
                "tree": (
                    "builds[number,result,timestamp,duration,building,url]"
                    f"{{0,{limit}}}"
                )
            },
        )
        assert response is not None
        return response.json().get("builds", [])[:limit]

    async def get_previous_successful_build(self, job: JobPath) -> dict[str, Any]:
        response = await self._request(
            f"{self.base_url}{self._job_path(job)}/lastSuccessfulBuild/api/json"
        )
        assert response is not None
        return response.json()