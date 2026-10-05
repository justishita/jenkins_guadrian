"""Environment-driven configuration for the Code & Remediation agent.

**Owner:** P3. Validated at startup so a missing token or repository fails the
container immediately rather than halfway through the first incident.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CodeAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitHub access. The token needs read access to contents and pull requests; write
    # access is only required from Week 5, when draft PRs are created.
    GITHUB_TOKEN: str = Field(min_length=1)
    GITHUB_REPO: str = Field(min_length=3, description="owner/name of the repository under CI")
    GITHUB_API_URL: str = "https://api.github.com"
    GITHUB_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=60)
    GITHUB_MAX_RETRIES: int = Field(default=3, ge=0, le=10)
    GITHUB_BACKOFF_SECONDS: float = Field(default=0.5, ge=0, le=30)

    RABBITMQ_URL: str = Field(min_length=1)
    RABBITMQ_CONNECT_RETRIES: int = Field(default=8, ge=0, le=20)
    RABBITMQ_CONNECT_BACKOFF_SECONDS: float = Field(default=1.0, ge=0, le=30)

    EXCHANGE_NAME: str = "incidents"
    # Each agent binds its own queue to the shared routing key so all three receive
    # their own copy of every incident.
    QUEUE_NAME: str = "code_agent.incident.created"
    ROUTING_KEY: str = "incident.created"

    EVIDENCE_DIR: str = "data/evidence"
    AUDIT_DIR: str = "data/audit"
    DATABASE_URL: str = ""

    OPA_URL: str = "http://opa:8181"
    POLICY_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=60)

    # How far back to look for the change that broke the build. Beyond this the
    # "recent change" hypothesis stops being credible anyway.
    COMMIT_LOOKBACK: int = Field(default=10, gt=0, le=100)
    MAX_DIFF_BYTES: int = Field(default=200_000, gt=0)

    LOG_LEVEL: str = "INFO"

    @field_validator("GITHUB_REPO")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Accept ``owner/name``; a full URL is a common and confusing mistake."""
        parts = value.strip().strip("/").split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("GITHUB_REPO must be in 'owner/name' form, not a URL")
        return "/".join(parts)

    @field_validator("GITHUB_API_URL", "OPA_URL")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


def load_settings() -> CodeAgentSettings:
    """Load settings; raises pydantic.ValidationError if required env vars are missing."""
    return CodeAgentSettings()  # type: ignore[call-arg]
