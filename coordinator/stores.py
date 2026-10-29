"""Read evidence from every place an agent may have written it.

**Owner:** P2 (Week 5). The agents do not all write to the same store. The Jenkins agent writes to
the blackboard (``data/blackboard``); the Metrics and Code agents write to the evidence directory
(``data/evidence``) and, when triggered over A2A, mirror their evidence onto the blackboard too. A
Coordinator that reads only one of them reports a healthy agent as missing - which the completeness
rule then (correctly) treats as an incomplete investigation. Reading both, newest document per agent,
removes that failure mode without changing any agent.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from common.models import Evidence

LOGGER = logging.getLogger(__name__)


class MergedEvidenceStore:
    """An ``EvidenceStore`` view over several stores; the newest evidence per agent wins."""

    def __init__(self, *stores) -> None:
        if not stores:
            raise ValueError("at least one evidence store is required")
        self._stores = stores
        #: Stores that failed on the last ``read_all`` per incident: an unreadable store is not an absent agent.
        self.read_errors: dict[str, list[str]] = {}

    async def write(self, evidence: Evidence) -> None:
        """Writes go to the first (primary) store only; reads merge them all."""
        await self._stores[0].write(evidence)

    async def read_all(self, incident_id: UUID | str) -> list[Evidence]:
        results = await asyncio.gather(*(self._read_all(store, incident_id) for store in self._stores))
        batches = [batch for batch, _ in results]
        failed = [message for _, message in results if message]
        self.read_errors[str(incident_id)] = failed
        for message in failed:
            LOGGER.warning("evidence store unreadable for incident %s: %s", incident_id, message)
        newest: dict[str, Evidence] = {}
        for batch in batches:
            for item in batch:
                current = newest.get(item.agent)
                if current is None or item.created_at > current.created_at:
                    newest[item.agent] = item
        return sorted(newest.values(), key=lambda item: item.agent)

    async def read(self, incident_id: UUID | str, agent: str) -> Evidence | None:
        found = [item for item in await self.read_all(incident_id) if item.agent == agent]
        return found[0] if found else None

    @staticmethod
    async def _read_all(store, incident_id: UUID | str) -> tuple[list[Evidence], str | None]:
        try:
            return list(await store.read_all(incident_id)), None
        except Exception as error:  # noqa: BLE001 - one unreadable store must not hide the others
            return [], f"{type(store).__name__}: {type(error).__name__}"


__all__ = ["MergedEvidenceStore"]
