"""Persisted report revisions: idempotent, attempt-aware, and repairable.

**Owner:** P2 (Week 5). A *revision* is one persisted synthesis of an incident's evidence.
The first one is the initial report; a later one exists only because the evidence set changed
(a late agent, a re-run). Identity is ``report_revision_id = <incident>:<attempt>:<fingerprint>``
where the fingerprint hashes the evidence the revision was built from, so:

* persisting the same evidence twice is a no-op (a duplicate message or a recovery re-run),
* a result belonging to an older attempt can never replace the current attempt's report,
* the revision file doubles as an outbox: the audit event is written after it, and
  ``missing_audit`` finds revisions whose event was lost so it can be repaired without
  creating another revision.

Reports and the audit trail live in different stores, so there is no transaction. The
guarantee is therefore at-least-once audit, de-duplicated by ``report_revision_id``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import UUID

from common.models import Evidence
from coordinator.synthesis import IncidentReport

_NAME = re.compile(r"^a(?P<attempt>\d+)-r(?P<revision>\d+)\.json$")


class PersistResult(str, Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True)
class Revision:
    report_revision_id: str
    incident_id: UUID
    attempt: int
    revision: int
    fingerprint: str
    reason: str
    created_at: datetime
    report: IncidentReport

    @property
    def event_type(self) -> str:
        """The first revision of an attempt is the initial synthesis; later ones are revisions."""
        return "synthesis_completed" if self.revision == 1 else "synthesis_revised"


def evidence_fingerprint(evidence: Iterable[Evidence]) -> str:
    """Hash of what each agent said and when; changes exactly when the evidence set does."""
    rows = sorted(
        (
            item.agent,
            item.status,
            item.failure_type.value,
            round(item.confidence, 6),
            item.created_at.isoformat(),
            len(item.evidence_items),
        )
        for item in evidence
    )
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()[:16]


def revision_id(incident_id: UUID | str, attempt: int, fingerprint: str) -> str:
    return f"{UUID(str(incident_id))}:{attempt}:{fingerprint}"


class RevisionStore:
    """File-backed revisions under ``<root>/<incident_id>/a<attempt>-r<revision>.json``."""

    def __init__(self, root: str | Path = Path("data") / "reports") -> None:
        self.root = Path(root)
        self._lock = asyncio.Lock()

    def _directory(self, incident_id: UUID | str) -> Path:
        return self.root / str(UUID(str(incident_id)))

    async def persist(
        self,
        report: IncidentReport,
        *,
        attempt: int,
        fingerprint: str,
        reason: str,
        now: datetime | None = None,
    ) -> tuple[PersistResult, Revision]:
        """Persist ``report`` as a new revision unless that evidence is already applied."""
        async with self._lock:
            return await asyncio.to_thread(self._persist_sync, report, attempt, fingerprint, reason, now)

    async def revisions(self, incident_id: UUID | str) -> list[Revision]:
        async with self._lock:
            return await asyncio.to_thread(self._load, incident_id)

    async def latest(self, incident_id: UUID | str) -> Revision | None:
        found = await self.revisions(incident_id)
        return found[-1] if found else None

    async def incidents(self) -> list[UUID]:
        def scan() -> list[UUID]:
            if not self.root.is_dir():
                return []
            ids = []
            for path in self.root.iterdir():
                try:
                    ids.append(UUID(path.name))
                except ValueError:
                    continue  # the legacy <id>.json / <id>.md report files
            return ids

        return await asyncio.to_thread(scan)

    def _persist_sync(
        self, report: IncidentReport, attempt: int, fingerprint: str, reason: str, now: datetime | None
    ) -> tuple[PersistResult, Revision]:
        existing = self._load(report.incident_id)
        if existing:
            newest_attempt = max(item.attempt for item in existing)
            if attempt < newest_attempt:
                return PersistResult.STALE, existing[-1]
            for item in existing:
                if item.attempt == attempt and item.fingerprint == fingerprint:
                    return PersistResult.DUPLICATE, item
        same_attempt = [item for item in existing if item.attempt == attempt]
        number = same_attempt[-1].revision + 1 if same_attempt else 1
        revision = Revision(
            report_revision_id=revision_id(report.incident_id, attempt, fingerprint),
            incident_id=report.incident_id,
            attempt=attempt,
            revision=number,
            fingerprint=fingerprint,
            reason=reason,
            created_at=(now or datetime.now(timezone.utc)),
            report=report,
        )
        self._write(revision)
        return PersistResult.CREATED, revision

    def _load(self, incident_id: UUID | str) -> list[Revision]:
        directory = self._directory(incident_id)
        if not directory.is_dir():
            return []
        loaded = []
        for path in directory.iterdir():
            if not _NAME.match(path.name):
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            loaded.append(
                Revision(
                    report_revision_id=document["report_revision_id"],
                    incident_id=UUID(document["incident_id"]),
                    attempt=int(document["attempt"]),
                    revision=int(document["revision"]),
                    fingerprint=document["fingerprint"],
                    reason=document["reason"],
                    created_at=datetime.fromisoformat(document["created_at"]),
                    report=IncidentReport.model_validate(document["report"]),
                )
            )
        return sorted(loaded, key=lambda item: (item.attempt, item.revision))

    def _write(self, revision: Revision) -> None:
        directory = self._directory(revision.incident_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"a{revision.attempt}-r{revision.revision}.json"
        document = {
            "report_revision_id": revision.report_revision_id,
            "incident_id": str(revision.incident_id),
            "attempt": revision.attempt,
            "revision": revision.revision,
            "fingerprint": revision.fingerprint,
            "reason": revision.reason,
            "created_at": revision.created_at.isoformat(),
            "report": json.loads(revision.report.model_dump_json()),
        }
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temp = Path(handle.name)
                handle.write(json.dumps(document, indent=2, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except Exception:
            if temp is not None:
                temp.unlink(missing_ok=True)
            raise


__all__ = [
    "PersistResult",
    "Revision",
    "RevisionStore",
    "evidence_fingerprint",
    "revision_id",
]
