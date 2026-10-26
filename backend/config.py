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

    # Web UI (accounts, projects, review). ``UI_SESSION_SECRET`` unset => login is disabled (fail closed).
    UI_SESSION_SECRET: str = Field(default="", description="HMAC key for session cookies; at least 32 characters")
    UI_SESSION_HOURS: int = Field(default=12, gt=0, le=720)
    UI_COOKIE_NAME: str = "jg_session"
    UI_COOKIE_SECURE: bool = Field(default=False, description="Set true behind HTTPS so the cookie is never sent in clear")
    UI_ADMIN_EMAIL: str = Field(default="", description="Seeds the first admin when no users exist")
    UI_ADMIN_PASSWORD: str = Field(default="", description="Password for the seeded admin; never logged")
    UI_BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=15, description="bcrypt cost; tests lower it")
    UI_TRUST_PROXY: bool = Field(default=False, description="Read the client address from X-Real-IP (set only behind our nginx)")
    UI_LOGIN_MAX_ATTEMPTS: int = Field(default=5, gt=0)
    UI_LOGIN_WINDOW_SECONDS: int = Field(default=300, gt=0)
    GRAFANA_PUBLIC_URL: str = Field(default="http://localhost:3000", description="Where a browser can reach Grafana")
    REPORTS_DIR: str = Field(default="data/reports", description="Coordinator report revisions, read-only here")

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
