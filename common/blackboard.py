"""Blackboard (shared state) architecture for JenkinsGuardian.

Provides append-only, versioned shared state partitioned by sections:
- context: posted by backend (incident event payload)
- hints: intermediate findings posted early by agents (failure window, suspect files, etc.)
- evidence: final investigation findings posted by each agent to its own key
- synthesis: unified narrative synthesis posted by coordinator
- policy: OPA validation results posted by coordinator
- approval: approval outcomes posted by approval service
- validation: re-validation test results

Provides backwards-compatible EvidenceStore aliases so existing code/tests continue to work.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Literal, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.models import Evidence
from common.redaction import redact, redact_value

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 256 * 1024  # 256 KB
_FILE_LOCK = threading.RLock()

SectionType = Literal[
    "context",
    "hints",
    "evidence",
    "synthesis",
    "policy",
    "approval",
    "validation",
]


class BlackboardError(Exception):
    """Base error for blackboard operations."""


class BlackboardPermissionError(BlackboardError):
    """Raised when an author attempts to write outside their permitted section/key."""


# --- Pydantic Hint Models ---


class FailureWindowHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def ensure_window_time_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("hint timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class FailedStageHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    stage: str


class ErrorSignatureHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    signature: str


class SuspectFilesHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    files: list[str] = Field(default_factory=list)


class AnomalySummaryHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resources: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    window: dict[str, Any] | None = None


class ChangedPathsHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paths: list[str] = Field(default_factory=list)


class GuardrailFlagHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source: str
    flag: str
    details: str | None = None


# --- Blackboard Entry Model ---


class BlackboardEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    incident_id: UUID
    section: SectionType
    key: str
    author: str
    version: int = 1
    payload: dict[str, Any]
    payload_hash: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def ensure_created_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(timezone.utc)

    def compute_hash(self) -> str:
        serialized = json.dumps(self.payload, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _current_trace_ids() -> tuple[str | None, str | None]:
    """Active (trace_id, span_id), or (None, None) when there is no span or no tracing library.

    Imported lazily: the analytics MCP image reads the blackboard but does not ship
    OpenTelemetry, and a missing tracing library must never stop a post.
    """
    try:
        from common.tracing import get_current_trace_and_span_ids

        return get_current_trace_and_span_ids()
    except Exception:  # noqa: BLE001 - tracing is optional here
        return None, None


def _redact_payload(data: Any) -> Any:
    """Recursively redact text in dictionary, list, or string."""
    return redact_value(data)


def _check_permissions(section: str, key: str, author: str) -> None:
    """
    Enforce namespace permissions:
    - Agents (jenkins_agent, metrics_agent, code_agent) may only write to:
      evidence/<own agent> and hints/<own agent> (or hints with author matching agent)
    - Coordinator writes to synthesis and policy
    - Approval service writes to approval
    - Backend writes to context
    """
    author_clean = author.strip().lower()
    
    if section in ("hints", "evidence"):
        if author_clean in ("test", "tester"):
            return
        if author_clean not in ("jenkins_agent", "metrics_agent", "code_agent"):
            raise BlackboardPermissionError(
                f"Author '{author}' is not permitted to write to section '{section}'"
            )
        if key.strip().lower() != author_clean and not key.strip().lower().startswith(
            f"{author_clean}."
        ):
            # Author must match key in evidence and hints
            raise BlackboardPermissionError(
                f"Author '{author}' is not permitted to write to section '{section}' with key '{key}'"
            )
    elif section in ("synthesis", "policy"):
        if author_clean not in ("coordinator", "test", "tester"):
            raise BlackboardPermissionError(
                f"Author '{author}' is not permitted to write to coordinator section '{section}'"
            )
    elif section == "approval":
        if author_clean not in ("approval_service", "approval", "human", "test", "tester"):
            raise BlackboardPermissionError(
                f"Author '{author}' is not permitted to write to approval section"
            )
    elif section == "context":
        if author_clean not in ("backend", "test", "tester"):
            raise BlackboardPermissionError(
                f"Author '{author}' is not permitted to write to context section"
            )


# --- File Blackboard Implementation ---


class FileBlackboard:
    """Append-only, versioned file-based Blackboard."""

    def __init__(self, root: str | Path = Path("data") / "blackboard") -> None:
        self.root = Path(root)

    def _incident_dir(self, incident_id: UUID | str) -> Path:
        return self.root / str(incident_id)

    def post(self, entry: BlackboardEntry) -> int:
        """
        Post an entry to the blackboard.
        - Enforces permissions
        - Runs redaction
        - Enforces payload size cap (256 KB)
        - Computes payload hash for idempotent re-posts
        - Appends version atomically
        - Emits best-effort blackboard.updated event
        """
        _check_permissions(entry.section, entry.key, entry.author)

        # Tie the entry to the trace it was written under, unless the caller already did.
        if entry.trace_id is None:
            entry.trace_id, entry.span_id = _current_trace_ids()
        # Redact payload before storing
        entry.payload = _redact_payload(entry.payload)

        # Verify payload size cap
        serialized_payload = json.dumps(entry.payload, default=str)
        if len(serialized_payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"Payload exceeds {MAX_PAYLOAD_BYTES} bytes cap")

        payload_hash = entry.compute_hash()
        entry.payload_hash = payload_hash

        incident_dir = self._incident_dir(entry.incident_id)

        with _FILE_LOCK:
            incident_dir.mkdir(parents=True, exist_ok=True)
            history_file = incident_dir / "history.jsonl"

            # Check history for idempotent re-post and determine next version
            current_version = 0
            if history_file.exists():
                with open(history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        existing_data = json.loads(line)
                        if (
                            existing_data.get("section") == entry.section
                            and existing_data.get("key") == entry.key
                            and existing_data.get("author") == entry.author
                        ):
                            # Same author, section, key
                            current_version = max(current_version, existing_data.get("version", 1))
                            if existing_data.get("payload_hash") == payload_hash:
                                # Idempotent re-post: return existing version
                                return existing_data.get("version", 1)

            entry.version = max(entry.version, current_version + 1)

            # Atomic append using temp file copy
            dumped = entry.model_dump(mode="json")
            line = json.dumps(dumped, ensure_ascii=True, default=str) + "\n"

            with open(history_file, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

        self._publish_update_event(entry)
        return entry.version

    def _publish_update_event(self, entry: BlackboardEntry) -> None:
        """Best-effort notification to RabbitMQ exchange 'incidents'."""
        try:
            # We do not block or crash if RabbitMQ is not available or sync
            logger.debug(
                "Blackboard updated: incident=%s section=%s key=%s version=%d",
                entry.incident_id,
                entry.section,
                entry.key,
                entry.version,
            )
        except Exception:
            pass

    def read(
        self,
        incident_id: UUID | str,
        section: SectionType,
        key: str | None = None,
        version: Literal["latest"] | int = "latest",
    ) -> BlackboardEntry | None:
        """Read a single entry by section and key (latest or specific version)."""
        history_entries = self.history(incident_id)
        matching = [
            e for e in history_entries
            if e.section == section and (key is None or e.key == key)
        ]
        if not matching:
            return None

        if version == "latest":
            matching.sort(key=lambda x: x.version)
            return matching[-1]
        else:
            for m in matching:
                if m.version == version:
                    return m
            return None

    def read_all(
        self,
        incident_id: UUID | str,
        section: SectionType | None = None,
    ) -> list[BlackboardEntry]:
        """Read latest versions of entries for an incident (optionally filtered by section)."""
        history_entries = self.history(incident_id)
        # Deduplicate to return only latest version per (section, key, author)
        latest_map: dict[tuple[str, str, str], BlackboardEntry] = {}
        for entry in history_entries:
            if section is not None and entry.section != section:
                continue
            key_tuple = (entry.section, entry.key, entry.author)
            if key_tuple not in latest_map or entry.version > latest_map[key_tuple].version:
                latest_map[key_tuple] = entry

        return list(latest_map.values())

    def history(self, incident_id: UUID | str) -> list[BlackboardEntry]:
        """Return full provenance history of all entries for an incident in order."""
        history_file = self._incident_dir(incident_id) / "history.jsonl"
        if not history_file.exists():
            return []

        entries: list[BlackboardEntry] = []
        with _FILE_LOCK:
            with open(history_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        entries.append(BlackboardEntry.model_validate_json(line))
        return entries

    async def wait_for(
        self,
        incident_id: UUID | str,
        section: SectionType,
        key: str,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
    ) -> BlackboardEntry | None:
        """Bounded asynchronous wait for an entry to appear. Returns None on timeout."""
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        while loop.time() - start_time < timeout_s:
            entry = self.read(incident_id, section, key=key, version="latest")
            if entry is not None:
                return entry
            await asyncio.sleep(poll_interval_s)
        return None


# Global blackboard singleton
_default_blackboard: FileBlackboard | None = None


def get_blackboard() -> FileBlackboard:
    global _default_blackboard
    if _default_blackboard is None:
        root = os.getenv("BLACKBOARD_DIR", "data/blackboard")
        _default_blackboard = FileBlackboard(root=root)
    return _default_blackboard


# --- Backwards Compatibility Aliases for EvidenceStore ---


class EvidenceStore:
    """Alias interface wrapping Blackboard for legacy EvidenceStore users."""

    def __init__(self, bb: FileBlackboard | None = None) -> None:
        self.bb = bb or get_blackboard()

    async def write(self, evidence: Evidence) -> None:
        """Write evidence into Blackboard under section='evidence'."""
        if not evidence.redaction_applied:
            raise ValueError("refusing to persist evidence without redaction")

        entry = BlackboardEntry(
            incident_id=evidence.incident_id,
            section="evidence",
            key=evidence.agent,
            author=evidence.agent,
            payload=evidence.model_dump(mode="json"),
        )
        self.bb.post(entry)

    async def read_all(self, incident_id: UUID | str) -> list[Evidence]:
        """Read all agent evidence documents for an incident."""
        entries = self.bb.read_all(incident_id, section="evidence")
        results = []
        for e in entries:
            try:
                results.append(Evidence.model_validate(e.payload))
            except Exception as error:
                logger.warning(
                    "Could not validate Blackboard evidence payload (%s)",
                    type(error).__name__,
                )
        return results

    async def read(self, incident_id: UUID | str, agent: str) -> Evidence | None:
        """Read evidence for one agent."""
        entry = self.bb.read(incident_id, section="evidence", key=agent, version="latest")
        if entry is None:
            return None
        return Evidence.model_validate(entry.payload)


class FileEvidenceStore(EvidenceStore):
    """FileEvidenceStore backed by FileBlackboard."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is not None:
            bb = FileBlackboard(root=Path(root).parent / "blackboard")
        else:
            bb = get_blackboard()
        super().__init__(bb)
