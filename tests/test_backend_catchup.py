from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.config import settings
from backend.jenkins_catchup import JenkinsCatchup, events_from_jenkins_payload


def build_payload(now: datetime) -> dict:
    stamp = int(now.timestamp() * 1000)
    return {
        "jobs": [
            {
                "name": "target-app",
                "fullName": "target-app",
                "url": "http://jenkins/job/target-app/",
                "jobs": [
                    {
                        "name": "main",
                        "fullName": "target-app/main",
                        "url": "http://jenkins/job/target-app/job/main/",
                        "builds": [
                            {
                                "number": 12,
                                "result": "FAILURE",
                                "timestamp": stamp,
                                "url": "http://jenkins/job/target-app/job/main/12/",
                                "actions": [{"lastBuiltRevision": {"SHA1": "abc123"}}],
                            },
                            {"number": 11, "result": "ABORTED", "timestamp": stamp},
                            {"number": 10, "result": "UNSTABLE", "timestamp": stamp},
                            {
                                "number": 9,
                                "result": "FAILURE",
                                "timestamp": int((now - timedelta(minutes=31)).timestamp() * 1000),
                            },
                        ],
                    }
                ],
            }
        ]
    }


def test_event_extraction_keeps_only_recent_failure_builds():
    now = datetime.now(timezone.utc)

    events = events_from_jenkins_payload(
        build_payload(now),
        cutoff=now - timedelta(minutes=30),
    )

    assert len(events) == 1
    event = events[0]
    assert event.job_name == "target-app/main"
    assert event.branch == "main"
    assert event.build_number == 12
    assert event.git_commit == "abc123"
    assert event.timestamp == datetime.fromtimestamp(
        int(now.timestamp() * 1000) / 1000,
        timezone.utc,
    )


class FakeSession:
    def __init__(self):
        self.added = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    def add(self, incident):
        self.added.append(incident)


@pytest.mark.asyncio
async def test_scan_creates_and_publishes_only_missing_recent_failures(monkeypatch):
    now = datetime.now(timezone.utc)
    body = build_payload(now)
    session = FakeSession()
    requested = []

    async def respond(request):
        requested.append(request)
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(settings, "JENKINS_URL", "http://jenkins")
    monkeypatch.setattr(settings, "JENKINS_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(settings, "CATCHUP_LOOKBACK_MINUTES", 30)
    with patch("backend.jenkins_catchup.async_session", return_value=session), patch(
        "backend.incidents.publisher.publish_incident_created",
        new_callable=AsyncMock,
        return_value=True,
    ) as publish:
        catchup = JenkinsCatchup(
            client_factory=lambda **kwargs: httpx.AsyncClient(
                transport=transport,
                **kwargs,
            ),
            clock=lambda: now,
        )

        created = await catchup.scan_once()

    assert created == 1
    assert len(session.added) == 1
    assert session.added[0].build_number == 12
    assert publish.await_count == 1
    assert publish.await_args.args[0]["job_name"] == "target-app/main"
    assert requested[0].url.path == "/api/json"
    assert "tree" in requested[0].url.params
