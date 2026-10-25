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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="set TEST_DATABASE_URL to run against PostgreSQL"),
]


async def create_audit_tables(url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    import backend.db.models  # noqa: F401  (registers the audit tables on the shared metadata)
    from backend.db.database import Base

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_audit_log_written_to_postgres():
    if not DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")

    await create_audit_tables(DATABASE_URL)

    audit = PostgresAuditLog(DATABASE_URL)
    event_writer = SharedStoreEvidenceWriter(FileEvidenceStore(Path("data/evidence")))

    agent = MetricsAgent(
        planner=CatalogPlanner(),
        investigator=Investigator(config=InvestigationConfig(tools=[FakeTool(LATENCY_P95, step_up())])),
        writer=event_writer,
        audit=audit,
    )

    incident_id = uuid4()
    event = IncidentCreatedEvent(
        incident_id=incident_id,
        job_name="orders-service",
        build_number=42,
        build_url="http://jenkins/job/orders-service/42",
        branch="main",
        git_commit="abc1234",
        timestamp=FAILURE_TIME,
    )

    await agent.investigate_and_write(event)

    conn = await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch(
            "SELECT event_type, summary, ok FROM agent_audit_records WHERE incident_id = $1 ORDER BY timestamp",
            str(incident_id),
        )
        assert len(rows) >= 2
        assert rows[0]["event_type"] == "investigation_started"
        assert rows[0]["ok"] is True
        assert rows[-1]["event_type"] == "investigation_completed"
        assert rows[-1]["ok"] is True
    finally:
        await conn.close()
