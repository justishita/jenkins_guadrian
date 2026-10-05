"""The Metrics agent's audit trail round-trips through a real PostgreSQL database.

Skipped unless `TEST_DATABASE_URL` is set, so the normal run needs no database:

    docker run -d --name pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=audit -p 55432:5432 postgres:16.4
    TEST_DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:55432/audit pytest tests/metrics_agent/test_audit_postgres.py

The audit tables are created the way the backend does (`Base.metadata.create_all`).
"""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from agents.metrics_agent.consumer import MetricsAgent
from agents.metrics_agent.investigator import InvestigationConfig, Investigator
from agents.metrics_agent.models import IncidentCreatedEvent
from agents.metrics_agent.planner import CatalogPlanner
from agents.metrics_agent.queries import LATENCY_P95
from agents.metrics_agent.store import SharedStoreEvidenceWriter
from common.audit import PostgresAuditLog
from common.evidence_store import FileEvidenceStore
from tests.metrics_agent.helpers import (
    FAILURE_TIME,
    FakeTool,
    make_window,
    step_up,
)

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="set TEST_DATABASE_URL to run against PostgreSQL")


async def create_audit_tables(url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    import backend.db.models  # noqa: F401  (registers the audit tables on the shared metadata)
    from backend.db.database import Base

    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def delete_trail(url: str, incident_id: str) -> None:
    connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await connection.execute("DELETE FROM audit_events WHERE incident_id = $1", incident_id)
    finally:
        await connection.close()


def test_investigation_trail_round_trips_through_postgres(tmp_path: Path) -> None:
    incident_id = uuid4()
    event = IncidentCreatedEvent.model_validate(
        {"incident_id": str(incident_id), "timestamp": FAILURE_TIME, "failed_stage": "Test"}
    )

    async def scenario() -> None:
        await create_audit_tables(DATABASE_URL)
        audit = PostgresAuditLog(DATABASE_URL)
        investigator = Investigator(
            FakeTool({LATENCY_P95.promql: step_up(make_window(), 0.007, 2.0)}),  # type: ignore[arg-type]
            CatalogPlanner(),
            SharedStoreEvidenceWriter(FileEvidenceStore(tmp_path)),
            InvestigationConfig(settle=timedelta(0)),
            # Real clock: the trail is ordered by timestamp, so every record must use real time.
            sleep=lambda _seconds: None,
        )
        try:
            evidence = await MetricsAgent(investigator, audit).run_event(event)
            records = await audit.read(incident_id)
        finally:
            await audit.close()
            await delete_trail(DATABASE_URL, str(incident_id))

        types = [r.event_type for r in records]
        assert types[0] == "agent_started" and types[-1] == "agent_completed"
        assert types[-3:-1] == ["hypothesis_formed", "evidence_written"]
        calls = [r for r in records if r.event_type == "tool_call"]
        assert len(calls) == len(evidence.tool_calls) > 0
        # Each query keeps its own issue time through the timestamptz round trip, in order.
        assert [c.created_at.isoformat() for c in calls] == [t.args["issued_at"] for t in evidence.tool_calls]
        assert [c.created_at for c in calls] == sorted(c.created_at for c in calls)
        assert records[-1].payload["failure_type"] == "timeout"

    asyncio.run(scenario())
