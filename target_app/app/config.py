"""Load orders service settings from YAML with environment overrides."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str | None = None
    database_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    enable_slow: bool = False
    enable_cpu: bool = False
    enable_leak: bool = False
    slow_max_delay_seconds: float = Field(default=10.0, ge=0, le=60)
    cpu_max_seconds: float = Field(default=5.0, ge=0, le=30)
    leak_chunk_bytes: int = Field(default=1_048_576, ge=1, le=16_777_216)
    leak_max_bytes: int = Field(default=67_108_864, ge=1)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("postgres://", "postgresql://")):
            raise ValueError("DATABASE_URL must use postgres:// or postgresql://")
        return value


ENVIRONMENT_OVERRIDES = {
    "database_url": "DATABASE_URL",
    "database_timeout_seconds": "ORDERS_DATABASE_TIMEOUT_SECONDS",
    "enable_slow": "ORDERS_ENABLE_SLOW",
    "enable_cpu": "ORDERS_ENABLE_CPU",
    "enable_leak": "ORDERS_ENABLE_LEAK",
    "slow_max_delay_seconds": "ORDERS_SLOW_MAX_DELAY_SECONDS",
    "cpu_max_seconds": "ORDERS_CPU_MAX_SECONDS",
    "leak_chunk_bytes": "ORDERS_LEAK_CHUNK_BYTES",
    "leak_max_bytes": "ORDERS_LEAK_MAX_BYTES",
}


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    """Read YAML settings and override supplied values from the environment."""
    raw_config: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError("Settings YAML must contain a mapping")

    values = dict(raw_config)
    for field_name, environment_name in ENVIRONMENT_OVERRIDES.items():
        if environment_name in os.environ:
            environment_value = os.environ[environment_name]
            values[field_name] = (
                None
                if field_name == "database_url" and not environment_value.strip()
                else environment_value
            )
    return Settings.model_validate(values)