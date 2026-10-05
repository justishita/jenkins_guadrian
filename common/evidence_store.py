"""PostgreSQL persistence for versioned, validated agent evidence."""

from __future__ import annotations

import asyncpg

from common.models import Evidence


class EvidenceStore:
    """Upsert one Evidence document per incident and agent."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL must be configured")
        self._database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Create the connection pool and initialize the evidence table."""
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._database_url,
            min_size=1,
            max_size=5,
            timeout=5,
            command_timeout=15,
        )
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_evidence (
                incident_id UUID NOT NULL,
                agent TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (incident_id, agent)
            )
            """
        )

    async def write(self, evidence: Evidence) -> None:
        """Persist evidence only after its required text redaction was applied."""
        if not evidence.redaction_applied:
            raise ValueError("refusing to persist evidence without redaction")
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO agent_evidence (incident_id, agent, schema_version, payload, created_at)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (incident_id, agent) DO UPDATE SET
                schema_version = EXCLUDED.schema_version,
                payload = EXCLUDED.payload,
                created_at = EXCLUDED.created_at
            """,
            evidence.incident_id,
            evidence.agent,
            evidence.schema_version,
            evidence.model_dump_json(exclude_none=True),
            evidence.created_at,
        )

    async def close(self) -> None:
        """Close the pool if it was initialized."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None