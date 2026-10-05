"""Typed models for data crossing the Prometheus tool and RabbitMQ boundaries."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Sample(BaseModel):
    """One (timestamp, value) point. Timestamps are timezone-aware UTC."""

    timestamp: datetime
    value: float


class Series(BaseModel):
    labels: dict[str, str]
    samples: list[Sample]


class QueryResult(BaseModel):
    """Normalised result of an instant or range query.

    `empty` is True when Prometheus answered successfully but returned no series;
    callers must treat that differently from an error.
    """

    promql: str
    result_type: Literal["vector", "matrix", "scalar", "string"]
    series: list[Series]

    @property
    def empty(self) -> bool:
        return not self.series


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class IncidentCreatedEvent(BaseModel):
    """The `incident.created` payload published by the backend webhook.

    Extra fields are ignored so the agent does not break when the backend
    payload grows; the official schema is owned by P3. `timestamp` is the
    failure time and anchors the investigation window.
    """

    model_config = ConfigDict(extra="ignore")

    incident_id: UUID
    job_name: str | None = None
    build_number: int | None = None
    build_url: str | None = None
    branch: str | None = None
    git_commit: str | None = None
    failed_stage: str | None = None
    remediation_attempt: int = Field(default=0, ge=0)
    timestamp: datetime | None = None
    received_at: datetime | None = None

    @field_validator("timestamp", "received_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)
