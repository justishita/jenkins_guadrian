"""Append-only audit trail shared by the agents, the Coordinator and the backend.

**Owner:** P3. Agents run in their own containers and must not depend on the backend's
SQLAlchemy session, so this mirrors ``common.evidence_store``: one small interface, a
file backend for local runs, and a PostgreSQL backend writing the tables defined in
``backend.db.audit_models``.

Two rules hold for every record written here:

* Nothing reaches the trail unredacted. ``AuditRecord`` redacts its own text, so a
  caller cannot forget.
* A record states that something happened at a point in time. Rows are never updated;
  a later outcome is a later record.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.redaction import redact


logger = logging.getLogger(__name__)

_FILE_IO_LOCK = threading.RLock()

#: A single payload value is capped so one oversized log excerpt cannot bloat the trail.
MAX_PAYLOAD_VALUE_CHARS = 4_000

#: ``common.redaction.redact`` works on text, so it only catches a secret written as
#: ``token=...`` inside a string. A payload is structured, and there the secret is the
#: *value* under a telling key - masked here whatever it looks like.
SECRET_KEY_RE = re.compile(
	r"(?i)(^|[_.-])(token|password|passwd|secret|api[_-]?key|apikey|authorization|auth|credential|cookie|session)s?([_.-]|$)"
)


def _utc_now() -> datetime:
	return datetime.now(timezone.utc)


def _redact_deep(value: Any) -> Any:
	"""Redact every string reachable in a JSON-shaped value, and cap long ones.

	A value stored under a secret-looking key is masked whole: unlike free text, its
	name already tells us it must never be written down.
	"""
	if isinstance(value, str):
		redacted = redact(value)
		if len(redacted) > MAX_PAYLOAD_VALUE_CHARS:
			return redacted[:MAX_PAYLOAD_VALUE_CHARS] + "...[truncated]"
		return redacted
	if isinstance(value, dict):
		return {
			str(key): "[REDACTED]" if SECRET_KEY_RE.search(str(key)) else _redact_deep(item)
			for key, item in value.items()
		}
	if isinstance(value, (list, tuple)):
		return [_redact_deep(item) for item in value]
	return value


class AuditRecord(BaseModel):
	"""One thing that happened while handling one incident.

	``actor`` is the component responsible (``code_agent``, ``coordinator``,
	``backend``, or a reviewer identity for human decisions).
	"""

	model_config = ConfigDict(extra="forbid")

	incident_id: UUID
	actor: str = Field(min_length=1, max_length=64)
	event_type: str = Field(min_length=1, max_length=64)
	summary: str = ""
	payload: dict[str, Any] = Field(default_factory=dict)
	duration_ms: float | None = Field(default=None, ge=0)
	ok: bool | None = None
	created_at: datetime = Field(default_factory=_utc_now)

	@field_validator("created_at")
	@classmethod
	def ensure_utc(cls, value: datetime) -> datetime:
		if value.tzinfo is None:
			return value.replace(tzinfo=timezone.utc)
		return value.astimezone(timezone.utc)

	@field_validator("summary")
	@classmethod
	def redact_summary(cls, value: str) -> str:
		return redact(value)

	@field_validator("payload")
	@classmethod
	def redact_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
		return _redact_deep(value)


class AuditLog(ABC):
	"""Storage interface for the incident audit trail."""

	@abstractmethod
	async def record(self, record: AuditRecord) -> None:
		"""Append one record. Must not raise for a storage outage - see implementations."""

	@abstractmethod
	async def read(self, incident_id: UUID | str) -> list[AuditRecord]:
		"""Return an incident's records in the order they were written."""

	@asynccontextmanager
	async def agent_run(
		self,
		incident_id: UUID | str,
		actor: str,
		*,
		event_type_started: str = "agent_started",
		event_type_completed: str = "agent_completed",
		event_type_failed: str = "agent_failed",
	) -> AsyncIterator[dict[str, Any]]:
		"""Bracket an agent's work with started/completed records and a duration.

		Yields a mutable dict the caller adds result detail to (failure type,
		confidence, tool-call count); it becomes the completion record's payload.
		A failure is recorded and then re-raised - the audit trail must not swallow
		the error the caller needs to handle.
		"""
		identifier = UUID(str(incident_id))
		await self.record(
			AuditRecord(incident_id=identifier, actor=actor, event_type=event_type_started)
		)
		details: dict[str, Any] = {}
		started = time.monotonic()
		try:
			yield details
		except Exception as error:
			await self.record(
				AuditRecord(
					incident_id=identifier,
					actor=actor,
					event_type=event_type_failed,
					summary=f"{type(error).__name__}: {error}",
					payload=details,
					duration_ms=(time.monotonic() - started) * 1000,
					ok=False,
				)
			)
			raise
		await self.record(
			AuditRecord(
				incident_id=identifier,
				actor=actor,
				event_type=event_type_completed,
				payload=details,
				duration_ms=(time.monotonic() - started) * 1000,
				ok=True,
			)
		)


class FileAuditLog(AuditLog):
	"""Append records as JSON lines under ``./data/audit/<incident_id>.jsonl``.

	The local backend for development and for the agent containers until the
	PostgreSQL trail is wired up. Appends are serialized through a lock and flushed
	so a crashed worker still leaves a readable trail.
	"""

	def __init__(self, root: str | Path = Path("data") / "audit") -> None:
		self.root = Path(root)
		self._lock = asyncio.Lock()

	def _path(self, incident_id: UUID | str) -> Path:
		return self.root / f"{UUID(str(incident_id))}.jsonl"

	async def record(self, record: AuditRecord) -> None:
		async with self._lock:
			await asyncio.to_thread(self._append_sync, record)

	def _append_sync(self, record: AuditRecord) -> None:
		path = self._path(record.incident_id)
		line = json.dumps(record.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
		with _FILE_IO_LOCK:
			path.parent.mkdir(parents=True, exist_ok=True)
			with path.open("a", encoding="utf-8") as handle:
				handle.write(line + "\n")
				handle.flush()
				os.fsync(handle.fileno())

	async def read(self, incident_id: UUID | str) -> list[AuditRecord]:
		async with self._lock:
			return await asyncio.to_thread(self._read_sync, incident_id)

	def _read_sync(self, incident_id: UUID | str) -> list[AuditRecord]:
		path = self._path(incident_id)
		if not path.is_file():
			return []
		with _FILE_IO_LOCK:
			lines = path.read_text(encoding="utf-8").splitlines()
		return [AuditRecord.model_validate_json(line) for line in lines if line.strip()]


class PostgresAuditLog(AuditLog):
	"""Write the trail to the ``audit_events`` table owned by the Alembic migrations.

	This backend never issues DDL: the schema belongs to ``migrations/``, so a
	mismatch surfaces as a migration problem rather than as two diverging table
	definitions.
	"""

	def __init__(self, database_url: str) -> None:
		if not database_url:
			raise ValueError("DATABASE_URL must be configured")
		self._database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
		self._pool: Any | None = None
		self._connect_lock = asyncio.Lock()

	async def connect(self) -> None:
		"""Create the connection pool if it does not exist yet."""
		import asyncpg

		async with self._connect_lock:
			if self._pool is not None:
				return
			self._pool = await asyncpg.create_pool(
				self._database_url, min_size=1, max_size=5, timeout=5, command_timeout=15
			)

	async def record(self, record: AuditRecord) -> None:
		if self._pool is None:
			await self.connect()
		assert self._pool is not None
		await self._pool.execute(
			"""
			INSERT INTO audit_events
				(incident_id, actor, event_type, summary, payload, duration_ms, ok, created_at)
			VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
			""",
			str(record.incident_id),
			record.actor,
			record.event_type,
			record.summary,
			json.dumps(record.payload),
			record.duration_ms,
			record.ok,
			record.created_at,
		)

	async def read(self, incident_id: UUID | str) -> list[AuditRecord]:
		if self._pool is None:
			await self.connect()
		assert self._pool is not None
		rows = await self._pool.fetch(
			"""
			SELECT incident_id, actor, event_type, summary, payload, duration_ms, ok, created_at
			FROM audit_events WHERE incident_id = $1 ORDER BY created_at, id
			""",
			str(incident_id),
		)
		return [
			AuditRecord.model_validate(
				{
					**dict(row),
					"payload": json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
				}
			)
			for row in rows
		]

	async def close(self) -> None:
		if self._pool is not None:
			await self._pool.close()
			self._pool = None


class BestEffortAuditLog(AuditLog):
	"""Wrap a log so a storage outage degrades the trail instead of the investigation.

	An agent that cannot write its audit trail should still finish investigating and
	still write its evidence; the dropped record is logged loudly so the gap is
	visible rather than silent. Reads are not wrapped - a caller asking for the trail
	wants the real error.
	"""

	def __init__(self, inner: AuditLog) -> None:
		self._inner = inner

	async def record(self, record: AuditRecord) -> None:
		try:
			await self._inner.record(record)
		except Exception:
			logger.exception(
				"dropping audit record",
				extra={
					"incident_id": str(record.incident_id),
					"actor": record.actor,
					"event_type": record.event_type,
				},
			)

	async def read(self, incident_id: UUID | str) -> list[AuditRecord]:
		return await self._inner.read(incident_id)


def build_audit_log(
	*, database_url: str | None = None, root: str | Path | None = None, best_effort: bool = True
) -> AuditLog:
	"""Return the PostgreSQL trail when a database is configured, else the file trail."""
	inner: AuditLog
	if database_url:
		inner = PostgresAuditLog(database_url)
	else:
		inner = FileAuditLog(root or Path("data") / "audit")
	return BestEffortAuditLog(inner) if best_effort else inner


__all__ = [
	"AuditLog",
	"AuditRecord",
	"BestEffortAuditLog",
	"FileAuditLog",
	"PostgresAuditLog",
	"build_audit_log",
]
