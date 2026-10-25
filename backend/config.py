import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # API configuration
    API_TITLE: str = "AI-Powered DevOps Incident Investigation API"
    API_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Security
    WEBHOOK_SHARED_SECRET: str = Field(
        default="",
        description="Shared secret for Jenkins webhook authentication"
    )
    APPROVAL_API_TOKEN: str = Field(
        default="",
        description="Bearer token for the /approvals API; unset disables it (fail closed)"
    )

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres_password@postgres:5432/devops_audit",
        description="PostgreSQL connection string"
    )

    # RabbitMQ
    RABBITMQ_URL: str = Field(
        default="amqp://rabbit:rabbit_password@rabbitmq:5672/",
        description="RabbitMQ connection string"
    )

    # Polling & Catch-up
    CATCHUP_ENABLED: bool = Field(
        default=True,
        description="Whether catch-up background task is enabled"
    )
    JENKINS_URL: str = ""
    JENKINS_USER: str = ""
    JENKINS_API_TOKEN: str = ""
    CATCHUP_INTERVAL_SECONDS: float = Field(default=60.0, gt=0)
    CATCHUP_LOOKBACK_MINUTES: int = Field(default=30, gt=0)
    JENKINS_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)

    # Notifications & Approvals
    APPROVAL_SIGNING_SECRET: str = Field(
        default="",
        description="Secret key for HMAC-signed approval URLs. Falls back to WEBHOOK_SHARED_SECRET if empty."
    )
    BASE_URL: str = Field(
        default="http://localhost:8000",
        description="Base URL of backend service for approval links"
    )
    SLACK_WEBHOOK_URL: str = Field(
        default="",
        description="Slack incoming webhook URL"
    )
    TEAMS_WEBHOOK_URL: str = Field(
        default="",
        description="Microsoft Teams incoming webhook URL"
    )
    SMTP_HOST: str = Field(
        default="localhost",
        description="SMTP server host"
    )
    SMTP_PORT: int = Field(
        default=587,
        description="SMTP server port"
    )
    SMTP_USER: str = Field(
        default="",
        description="SMTP username"
    )
    SMTP_PASSWORD: str = Field(
        default="",
        description="SMTP password"
    )
    SMTP_USE_TLS: bool = Field(
        default=False,
        description="Use direct TLS for SMTP"
    )
    SMTP_STARTTLS: bool = Field(
        default=True,
        description="Use STARTTLS for SMTP"
    )
    NOTIFICATION_EMAIL_FROM: str = Field(
        default="alerts@jenkinsguardians.local",
        description="Sender email address"
    )
    NOTIFICATION_EMAIL_TO: str = Field(
        default="devops-team@jenkinsguardians.local",
        description="Recipient email address"
    )
    NOTIFICATION_MAX_RETRIES: int = Field(
        default=3,
        description="Max retries per notification channel"
    )
    NOTIFICATION_RETRY_BACKOFF: float = Field(
        default=1.0,
        description="Base backoff seconds for notification retries"
    )

    @property
    def async_database_url(self) -> str:
        """Ensure the URL uses the asyncpg driver."""
        url = self.DATABASE_URL
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
