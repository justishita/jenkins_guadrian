"""Typed models for data crossing the Prometheus tool and RabbitMQ boundaries."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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


class IncidentCreatedEvent(BaseModel):
    """Fields the Metrics agent needs from an `incident.created` message.

    Extra fields are ignored so the agent does not break when the backend
    payload grows; the official schema is owned by P3.
    """

    model_config = ConfigDict(extra="ignore")

    incident_id: UUID
    job_name: str | None = None
    build_number: int | None = None
    created_at: datetime | None = None
