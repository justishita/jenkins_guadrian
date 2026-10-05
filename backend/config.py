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
