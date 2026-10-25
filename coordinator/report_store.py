"""Where finished incident reports are kept: ``data/reports/<incident_id>.{json,md}``.

Reports are derived output, kept apart from the evidence blackboard so the agents'
original documents are never overwritten. Writes are atomic (temp file + replace); a
re-synthesis replaces the previous report for that incident.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from uuid import UUID

from coordinator.report import IncidentReport, render_markdown


class ReportStore:
    def __init__(self, root: str | Path = Path("data") / "reports") -> None:
        self.root = Path(root)

    def path(self, incident_id: UUID | str, suffix: str = "json") -> Path:
        return self.root / f"{UUID(str(incident_id))}.{suffix}"

    async def write(self, report: IncidentReport) -> Path:
        await asyncio.to_thread(self._write_sync, report)
        return self.path(report.incident_id)

    async def read(self, incident_id: UUID | str) -> IncidentReport | None:
        return await asyncio.to_thread(self._read_sync, incident_id)

    def _write_sync(self, report: IncidentReport) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic(self.path(report.incident_id, "md"), render_markdown(report))
        self._atomic(self.path(report.incident_id), report.model_dump_json(indent=2))

    def _read_sync(self, incident_id: UUID | str) -> IncidentReport | None:
        path = self.path(incident_id)
        if not path.is_file():
            return None
        return IncidentReport.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _atomic(path: Path, text: str) -> None:
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temp = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except Exception:
            if temp is not None:
                temp.unlink(missing_ok=True)
            raise


__all__ = ["ReportStore"]
