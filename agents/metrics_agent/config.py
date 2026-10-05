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

    RABBITMQ_CONNECT_RETRIES: int = Field(default=8, ge=0, le=20)
    RABBITMQ_CONNECT_BACKOFF_SECONDS: float = Field(default=1.0, ge=0, le=30)

    INVESTIGATION_LOOKBACK_SECONDS: int = Field(default=300, gt=0, le=86_400)
    INVESTIGATION_TAIL_SECONDS: int = Field(default=60, ge=0, le=3_600)
    INVESTIGATION_SETTLE_SECONDS: int = Field(default=30, ge=0, le=120)
    BASELINE_SECONDS: int = Field(default=600, gt=0, le=86_400)
    QUERY_STEP: str = Field(default="15s", pattern=r"^\d+[smh]$")

    # Root of the shared FileEvidenceStore (same default as the other agents).
    EVIDENCE_DIR: str = "data/evidence"

    # Audit trail: PostgreSQL when DATABASE_URL is set, otherwise JSON lines under AUDIT_DIR
    # (same selection as the other agents, via common.audit.build_audit_log).
    AUDIT_DIR: str = "data/audit"
    DATABASE_URL: str = ""

    EXCHANGE_NAME: str = "incidents"
    QUEUE_NAME: str = "metrics_agent.incident.created"
    ROUTING_KEY: str = "incident.created"
    LOG_LEVEL: str = "INFO"


def load_settings() -> MetricsAgentSettings:
    """Load settings; raises pydantic.ValidationError if required env vars are missing."""
    return MetricsAgentSettings()  # type: ignore[call-arg]
