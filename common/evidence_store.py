"""Shared evidence-store interface and file/PostgreSQL implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from uuid import UUID

import asyncpg

from common.models import Evidence


_FILE_IO_LOCK = threading.RLock()


def _parse_evidence_payload(payload: Any) -> Evidence:
    if isinstance(payload, str):
        payload = json.loads(payload)
    return Evidence.model_validate(payload)


class EvidenceStore(ABC):
    """Storage interface for one evidence document per incident and agent."""

    @abstractmethod
    async def write(self, evidence: Evidence) -> None:
        """Create or replace evidence for its (incident, agent) key."""

    @abstractmethod
    async def read_all(self, incident_id: UUID | str) -> list[Evidence]:
        """Return all agents' evidence for an incident."""

    @abstractmethod
    async def read(self, incident_id: UUID | str, agent: str) -> Evidence | None:
        """Return one agent's evidence for an incident, if present."""


class FileEvidenceStore(EvidenceStore):
    """Atomically persist validated evidence under ``./data/evidence``."""

    def __init__(self, root: str | Path = Path("data") / "evidence") -> None:
        self.root = Path(root)
        self._lock = asyncio.Lock()

    @staticmethod
    def _validate_redaction(evidence: Evidence) -> None:
        if not evidence.redaction_applied:
            raise ValueError("refusing to persist evidence without redaction")

    def _incident_directory(self, incident_id: UUID | str) -> Path:
        return self.root / str(UUID(str(incident_id)))

    @staticmethod
    def _validate_agent(agent: str) -> None:
        if not agent or Path(agent).name != agent or agent in {".", ".."}:
            raise ValueError("agent must be a non-empty file-name component")

    async def write(self, evidence: Evidence) -> None:
        """Atomically upsert the evidence and increment its stored version."""
        self._validate_redaction(evidence)
        self._validate_agent(evidence.agent)
        async with self._lock:
            await asyncio.to_thread(self._write_sync, evidence)

    def _write_sync(self, evidence: Evidence) -> None:
        path = self._incident_directory(evidence.incident_id) / f"{evidence.agent}.json"
        with _FILE_IO_LOCK:
            version = 1
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                version = int(existing["version"]) + 1

            path.parent.mkdir(parents=True, exist_ok=True)
            document = evidence.model_dump(mode="json", exclude_none=True)
            document["version"] = version
            serialized = json.dumps(
                document,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temp_path = Path(temporary.name)
                    temporary.write(serialized)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temp_path, path)
            except Exception:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                raise

    async def read_all(self, incident_id: UUID | str) -> list[Evidence]:
        """Read every agent file stored for an incident."""
        async with self._lock:
            return await asyncio.to_thread(self._read_all_sync, incident_id)

    def _read_all_sync(self, incident_id: UUID | str) -> list[Evidence]:
        directory = self._incident_directory(incident_id)
        if not directory.exists():
            return []
        with _FILE_IO_LOCK:
            return [
                self._read_path(path)
                for path in sorted(directory.glob("*.json"))
                if path.is_file()
            ]

    async def read(self, incident_id: UUID | str, agent: str) -> Evidence | None:
        """Read evidence for an incident and agent, or return ``None`` if absent."""
        self._validate_agent(agent)
        async with self._lock:
            return await asyncio.to_thread(self._read_sync, incident_id, agent)

    def _read_sync(self, incident_id: UUID | str, agent: str) -> Evidence | None:
        path = self._incident_directory(incident_id) / f"{agent}.json"
        if not path.is_file():
            return None
        with _FILE_IO_LOCK:
            return self._read_path(path)

    @staticmethod
    def _read_path(path: Path) -> Evidence:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("version"), int):
            raise ValueError(f"invalid evidence document in {path}")
        evidence_document = {key: value for key, value in document.items() if key != "version"}
        return Evidence.model_validate(evidence_document)


class PostgresEvidenceStore(EvidenceStore):
    """PostgreSQL implementation retained for deployments that need it."""

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
                version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (incident_id, agent)
            )
            """
        )
        await self._pool.execute(
            "ALTER TABLE agent_evidence ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1"
        )

    async def write(self, evidence: Evidence) -> None:
        """Persist redacted evidence and increment its version on each upsert."""
        FileEvidenceStore._validate_redaction(evidence)
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO agent_evidence
                (incident_id, agent, schema_version, payload, created_at, version)
            VALUES ($1, $2, $3, $4::jsonb, $5, 1)
            ON CONFLICT (incident_id, agent) DO UPDATE SET
                schema_version = EXCLUDED.schema_version,
                payload = EXCLUDED.payload,
                created_at = EXCLUDED.created_at,
                version = agent_evidence.version + 1
            """,
            evidence.incident_id,
            evidence.agent,
            evidence.schema_version,
            evidence.model_dump_json(exclude_none=True),
            evidence.created_at,
        )

    async def read_all(self, incident_id: UUID | str) -> list[Evidence]:
        """Read evidence documents for all agents attached to an incident."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        rows = await self._pool.fetch(
            "SELECT payload FROM agent_evidence WHERE incident_id = $1 ORDER BY agent",
            UUID(str(incident_id)),
        )
        return [_parse_evidence_payload(row["payload"]) for row in rows]

    async def read(self, incident_id: UUID | str, agent: str) -> Evidence | None:
        """Read one evidence document from PostgreSQL, if it exists."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            "SELECT payload FROM agent_evidence WHERE incident_id = $1 AND agent = $2",
            UUID(str(incident_id)),
            agent,
        )
        return _parse_evidence_payload(row["payload"]) if row is not None else None

    async def close(self) -> None:
        """Close the pool if it was initialized."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
