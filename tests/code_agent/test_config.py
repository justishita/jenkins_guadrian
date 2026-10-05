"""Tests for the Code agent's startup configuration.

**Owner:** P3. Configuration is validated at startup so the container dies on a
missing token instead of halfway through the first real incident - these tests pin
that behaviour down.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.code_agent.config import CodeAgentSettings, load_settings


REQUIRED = {
	"GITHUB_TOKEN": "ghp_token",
	"GITHUB_REPO": "justishita/jenkins_guadrian",
	"RABBITMQ_URL": "amqp://rabbit:rabbit@rabbitmq:5672/",
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
	"""Keep a developer's real .env and shell out of these assertions."""
	for name in (
		*REQUIRED,
		"GITHUB_API_URL",
		"OPA_URL",
		"COMMIT_LOOKBACK",
		"EVIDENCE_DIR",
		"QUEUE_NAME",
	):
		monkeypatch.delenv(name, raising=False)
	monkeypatch.chdir(tmp_path)


def test_settings_load_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
	for name, value in REQUIRED.items():
		monkeypatch.setenv(name, value)

	settings = load_settings()

	assert settings.GITHUB_REPO == "justishita/jenkins_guadrian"
	assert settings.QUEUE_NAME == "code_agent.incident.created"
	assert settings.ROUTING_KEY == "incident.created"
	assert settings.EXCHANGE_NAME == "incidents"


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_a_missing_required_setting_fails_at_startup(
	monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
	for name, value in REQUIRED.items():
		if name != missing:
			monkeypatch.setenv(name, value)

	with pytest.raises(ValidationError):
		load_settings()


@pytest.mark.parametrize(
	"repository",
	["https://github.com/owner/name", "owner", "owner/name/extra", "/name", "owner/"],
)
def test_a_repository_that_is_not_owner_slash_name_is_rejected(
	monkeypatch: pytest.MonkeyPatch, repository: str
) -> None:
	for name, value in REQUIRED.items():
		monkeypatch.setenv(name, value)
	monkeypatch.setenv("GITHUB_REPO", repository)

	with pytest.raises(ValidationError):
		load_settings()


def test_a_repository_with_surrounding_slashes_is_normalised(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	for name, value in REQUIRED.items():
		monkeypatch.setenv(name, value)
	monkeypatch.setenv("GITHUB_REPO", "/owner/name/")

	assert load_settings().GITHUB_REPO == "owner/name"


def test_urls_lose_their_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
	for name, value in REQUIRED.items():
		monkeypatch.setenv(name, value)
	monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com/")
	monkeypatch.setenv("OPA_URL", "http://opa:8181/")

	settings = load_settings()

	assert settings.GITHUB_API_URL == "https://api.github.com"
	assert settings.OPA_URL == "http://opa:8181"


@pytest.mark.parametrize(
	("field", "value"),
	[
		("GITHUB_TIMEOUT_SECONDS", "0"),
		("GITHUB_MAX_RETRIES", "-1"),
		("COMMIT_LOOKBACK", "0"),
		("COMMIT_LOOKBACK", "1000"),
		("MAX_DIFF_BYTES", "0"),
	],
)
def test_out_of_range_tuning_is_rejected(
	monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
	for name, default in REQUIRED.items():
		monkeypatch.setenv(name, default)
	monkeypatch.setenv(field, value)

	with pytest.raises(ValidationError):
		load_settings()


def test_the_queue_name_is_the_agents_own(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Each agent binds its own queue so all three get a copy of every incident."""
	for name, value in REQUIRED.items():
		monkeypatch.setenv(name, value)

	settings = CodeAgentSettings()  # type: ignore[call-arg]

	assert settings.QUEUE_NAME.startswith("code_agent.")
	assert settings.QUEUE_NAME != "metrics_agent.incident.created"
