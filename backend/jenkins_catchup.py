"""Recover recent Jenkins failures whose webhook incident was not recorded."""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.database import async_session
from backend.incidents import create_incident_if_missing
from backend.models.events import JenkinsFailureEvent


logger = logging.getLogger(__name__)
RETRY_LIMIT = 2
MAX_BACKOFF_SECONDS = 4.0
BUILD_FIELDS = (
    "number,result,timestamp,url,"
    "actions[lastBuiltRevision[SHA1]],changeSet[items[commitId]]"
)


def _jobs_tree(depth: int = 4) -> str:
    fields = f"name,fullName,url,_class,builds[{BUILD_FIELDS}]"
    for _ in range(depth - 1):
        fields = f"name,fullName,url,_class,builds[{BUILD_FIELDS}],jobs[{fields}]"
    return f"jobs[{fields}]"


def _git_commit(build: Mapping[str, Any]) -> str:
    for action in build.get("actions", []):
        revision = action.get("lastBuiltRevision", {}) if isinstance(action, Mapping) else {}
        sha = revision.get("SHA1") if isinstance(revision, Mapping) else None
        if sha:
            return str(sha)
    for item in build.get("changeSet", {}).get("items", []):
        if isinstance(item, Mapping) and item.get("commitId"):
            return str(item["commitId"])
    return ""


def events_from_jenkins_payload(
    payload: Mapping[str, Any],
    *,
    cutoff: datetime,
) -> list[JenkinsFailureEvent]:
    """Extract FAILURE build events newer than cutoff from a Jenkins jobs tree."""
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")

    events: list[JenkinsFailureEvent] = []
    seen: set[tuple[str, int, str]] = set()

    def visit(job: Mapping[str, Any], parent_path: tuple[str, ...] = ()) -> None:
        name = str(job.get("name") or "")
        full_name = str(job.get("fullName") or "/".join((*parent_path, name)))
        path = tuple(full_name.split("/")) if full_name else parent_path
        job_url = str(job.get("url") or "")

        builds = job.get("builds", [])
        if isinstance(builds, list):
            for build in builds:
                if not isinstance(build, Mapping) or build.get("result") != "FAILURE":
                    continue
                try:
                    build_number = int(build["number"])
                    timestamp = datetime.fromtimestamp(
                        int(build["timestamp"]) / 1000, tz=timezone.utc
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    logger.warning("Skipping Jenkins failure with invalid build metadata")
                    continue
                if timestamp < cutoff.astimezone(timezone.utc):
                    continue
                build_url = str(build.get("url") or f"{job_url.rstrip('/')}/{build_number}/")
                job_name = full_name or name
                identity = (job_name, build_number, build_url)
                if identity in seen:
                    continue
                seen.add(identity)
                events.append(
                    JenkinsFailureEvent(
                        job_name=job_name,
                        build_number=build_number,
                        build_url=build_url,
                        branch=path[-1] if path else name,
                        git_commit=_git_commit(build),
                        failed_stage=None,
                        timestamp=timestamp,
                        remediation_attempt=0,
                    )
                )

        children = job.get("jobs", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, Mapping):
                    visit(child, path)

    roots = payload.get("jobs", [])
    if isinstance(roots, list):
        for root in roots:
            if isinstance(root, Mapping):
                visit(root)
    return events


class JenkinsCatchup:
    """Poll Jenkins and persist/publish any missing recent FAILURE incidents."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession] | None = None,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.session_factory = session_factory or async_session
        self.client_factory = client_factory
        self.sleep = sleep
        self.clock = clock
        self._auth = (
            (settings.JENKINS_USER, settings.JENKINS_API_TOKEN)
            if settings.JENKINS_USER and settings.JENKINS_API_TOKEN
            else None
        )

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Mapping[str, Any]:
        for attempt in range(RETRY_LIMIT + 1):
            try:
                response = await client.get(url, params={"tree": _jobs_tree()})
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, Mapping):
                    raise ValueError("Jenkins API returned a non-object response")
                return data
            except (httpx.RequestError, httpx.HTTPStatusError) as error:
                status_code = (
                    error.response.status_code
                    if isinstance(error, httpx.HTTPStatusError)
                    else None
                )
                retryable = isinstance(error, httpx.RequestError) or (
                    status_code == 429
                    or (status_code is not None and 500 <= status_code <= 599)
                )
                if not retryable or attempt >= RETRY_LIMIT:
                    raise
                await self.sleep(min(2**attempt, MAX_BACKOFF_SECONDS))
        raise AssertionError("unreachable")

    async def scan_once(self) -> int:
        """Scan Jenkins once; return the number of newly recorded incidents."""
        if not settings.JENKINS_URL:
            raise ValueError("JENKINS_URL must be configured when catch-up is enabled")
        cutoff = self.clock() - timedelta(minutes=settings.CATCHUP_LOOKBACK_MINUTES)
        api_url = f"{settings.JENKINS_URL.rstrip('/')}/api/json"
        async with self.client_factory(
            timeout=settings.JENKINS_TIMEOUT_SECONDS,
            auth=self._auth,
        ) as client:
            payload = await self._get_json(client, api_url)

        events = events_from_jenkins_payload(payload, cutoff=cutoff)
        created = 0
        async with self.session_factory() as db:
            for event in events:
                try:
                    _, was_created = await create_incident_if_missing(event, db)
                    created += int(was_created)
                except Exception as error:
                    await db.rollback()
                    logger.error(
                        "Failed to record Jenkins catch-up incident",
                        extra={
                            "job_name": event.job_name,
                            "build_number": event.build_number,
                            "error_type": type(error).__name__,
                        },
                    )
        logger.info(
            "Jenkins catch-up scan complete",
            extra={"failure_builds_found": len(events), "incidents_created": created},
        )
        return created

    async def run(self) -> None:
        """Run catch-up scans at the configured interval until cancelled."""
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "Jenkins catch-up scan failed",
                    extra={"error_type": type(error).__name__},
                )
            await self.sleep(settings.CATCHUP_INTERVAL_SECONDS)
