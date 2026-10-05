"""Validated runtime settings for the Jenkins investigation worker."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class JenkinsAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    RABBITMQ_URL: SecretStr
    DATABASE_URL: SecretStr
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL: str = "gemini-2.5-flash"
    EXCHANGE_NAME: str = "incidents"
    QUEUE_NAME: str = "jenkins_agent.incident.created"
    ROUTING_KEY: str = "incident.created"
    TOTAL_TIMEOUT_SECONDS: float = Field(default=180.0, gt=0, le=600)
    PER_CALL_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0, le=120)
    LLM_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=120)
    LOG_LEVEL: str = "INFO"


def load_settings() -> JenkinsAgentSettings:
    """Load and validate worker settings from process environment or `.env`."""
    return JenkinsAgentSettings()  # type: ignore[call-arg]