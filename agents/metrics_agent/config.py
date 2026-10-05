"""Environment-driven configuration for the Metrics agent, validated at startup."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MetricsAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROMETHEUS_URL: str = Field(min_length=1)
    RABBITMQ_URL: str = Field(min_length=1)

    PROMETHEUS_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=60)
    PROMETHEUS_MAX_RETRIES: int = Field(default=3, ge=0, le=10)
    PROMETHEUS_BACKOFF_SECONDS: float = Field(default=0.5, ge=0, le=30)

    EXCHANGE_NAME: str = "incidents"
    QUEUE_NAME: str = "metrics_agent.incident.created"
    ROUTING_KEY: str = "incident.created"
    LOG_LEVEL: str = "INFO"


def load_settings() -> MetricsAgentSettings:
    """Load settings; raises pydantic.ValidationError if required env vars are missing."""
    return MetricsAgentSettings()  # type: ignore[call-arg]
