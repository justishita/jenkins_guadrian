"""Evidence persistence behind an interface.

Week 2 ships a local, schema-validating adapter. When P3's shared Evidence Store
exists, add an adapter implementing `EvidenceWriter`; agent logic does not change.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Protocol

from jsonschema import Draft7Validator

from common.models import Evidence

logger = logging.getLogger(__name__)

# Until P3 delivers common/evidence_schema.json (currently empty), use the agreed stub.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "common" / "evidence_schema_stub.json"


class EvidenceValidationError(ValueError):
    """The evidence record does not conform to the shared schema."""


class EvidenceWriter(Protocol):
    def write(self, record: Evidence) -> None: ...


class LocalJsonEvidenceWriter:
    """Validates against the shared schema and stores `<incident_id>.metrics_agent.json`.

    Re-delivery of the same incident overwrites the same file, so writes are idempotent.
    """

    def __init__(self, directory: Path, schema_path: Path = DEFAULT_SCHEMA_PATH) -> None:
        self._directory = directory
        self._validator = Draft7Validator(json.loads(schema_path.read_text(encoding="utf-8")))

    def write(self, record: Evidence) -> None:
        payload = record.model_dump(mode="json", exclude_none=True)
        errors = sorted(self._validator.iter_errors(payload), key=lambda e: list(e.path))
        if errors:
            raise EvidenceValidationError("; ".join(f"{list(e.path)}: {e.message}" for e in errors))

        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{record.incident_id}.{record.agent}.json"
        # Write-then-rename so readers never observe a half-written file.
        fd, tmp_name = tempfile.mkstemp(dir=self._directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        logger.info("evidence written", extra={"incident_id": str(record.incident_id), "path": str(target)})
