"""The webhook publishing path of scripts/inject_faults.py (no network: httpx.MockTransport)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from backend.models.events import JenkinsFailureEvent
from scripts import inject_faults as faults

FAULT_END = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
SECRET = "s3cr3t-webhook-value"


def api(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(base_url="http://localhost:8000", transport=httpx.MockTransport(handler))


def test_failure_fields_satisfy_the_backends_real_webhook_model() -> None:
    event = JenkinsFailureEvent.model_validate(faults.failure_fields(FAULT_END))
    assert event.job_name == "target-app/main" and event.failed_stage == "Deploy"
    assert event.timestamp == FAULT_END  # the incident is stamped with the end of the fault


def test_webhook_publish_sends_token_and_fields_and_returns_the_incident_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("x-webhook-token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"status": "accepted", "incident_id": "11111111-2222-3333-4444-555555555555"})

    incident_id = faults.publish_via_webhook(api(handler), SECRET, FAULT_END)

    assert incident_id == "11111111-2222-3333-4444-555555555555"
    assert seen["path"] == "/webhooks/jenkins" and seen["token"] == SECRET
    assert seen["body"]["timestamp"] == FAULT_END.isoformat()  # type: ignore[index]
    assert "incident_id" not in seen["body"]  # type: ignore[operator]  # the backend assigns it


def test_an_already_processed_build_still_returns_its_incident_id() -> None:
    client = api(lambda r: httpx.Response(200, json={"status": "already_processed", "incident_id": "abc"}))
    assert faults.publish_via_webhook(client, SECRET, FAULT_END) == "abc"


def test_a_rejected_token_exits_with_a_clear_message_that_does_not_leak_the_secret() -> None:
    client = api(lambda r: httpx.Response(401, json={"detail": "Invalid webhook token"}))
    with pytest.raises(SystemExit) as exit_info:
        faults.publish_via_webhook(client, SECRET, FAULT_END)
    message = str(exit_info.value)
    assert "401" in message and SECRET not in message


def test_a_server_error_exits_instead_of_pretending_the_incident_was_published() -> None:
    client = api(lambda r: httpx.Response(500, json={"detail": "Failed to record incident"}))
    with pytest.raises(SystemExit, match="HTTP 500"):
        faults.publish_via_webhook(client, SECRET, FAULT_END)


# --- the secret stays on this machine ---------------------------------------------------


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://host.docker.internal:8000"])
def test_local_hosts_are_allowed(url: str) -> None:
    faults.ensure_local(url, allow_remote=False)


def test_a_remote_host_is_refused_unless_explicitly_allowed() -> None:
    with pytest.raises(SystemExit, match="non-local host"):
        faults.ensure_local("https://example.com", allow_remote=False)
    faults.ensure_local("https://example.com", allow_remote=True)


def test_secret_comes_from_the_environment_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WEBHOOK_SHARED_SECRET=from-file\n", encoding="utf-8")
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "from-env")
    assert faults.load_webhook_secret(env_file) == "from-env"


def test_secret_falls_back_to_the_env_file_and_strips_quotes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WEBHOOK_SHARED_SECRET", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\nWEBHOOK_SHARED_SECRET='quoted-secret'\n", encoding="utf-8")
    assert faults.load_webhook_secret(env_file) == "quoted-secret"


def test_a_missing_secret_exits_with_a_clear_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WEBHOOK_SHARED_SECRET", raising=False)
    with pytest.raises(SystemExit, match="WEBHOOK_SHARED_SECRET is not set"):
        faults.load_webhook_secret(tmp_path / "missing.env")
