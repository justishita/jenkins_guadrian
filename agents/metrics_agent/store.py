"""Evidence persistence behind an interface.

The agent's pipeline is synchronous (blocking Prometheus calls, run in a worker
thread), while the shared `common.evidence_store` API is async. `EvidenceWriter` is
the seam: the investigator only ever calls `write()`.
"""

import asyncio
import logging
from typing import Protocol

from common.evidence_store import EvidenceStore
from common.models import Evidence

logger = logging.getLogger(__name__)


class EvidenceWriter(Protocol):
    def write(self, record: Evidence) -> None: ...


class SharedStoreEvidenceWriter:
    """Writes the agent's evidence into the shared Evidence Store.

    Evidence is stored under (incident, "metrics_agent"), so the Coordinator can read
    it next to the other agents' evidence with `read_all(incident_id)`. Re-delivery of
    the same incident replaces the document (the store bumps its version).

    `write()` must be called from a thread with no running event loop, which is how the
    consumer invokes the investigator (`asyncio.to_thread`).
    """

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def write(self, record: Evidence) -> None:
        asyncio.run(self._store.write(record))
        logger.info("evidence written", extra={"incident_id": str(record.incident_id), "agent": record.agent})
