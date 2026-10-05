"""Query planning. The agent asks a planner which catalog queries to run.

`CatalogPlanner` is deterministic. An LLM-backed planner (ReAct-style, built on
`common.llm_client` once P1 delivers it) can implement the same protocol later
without touching the investigator.
"""

from typing import Protocol

from .models import IncidentCreatedEvent
from .queries import AVAILABILITY, CATALOG, QuerySpec


class QueryPlanner(Protocol):
    def plan(self, event: IncidentCreatedEvent) -> list[QuerySpec]: ...


class CatalogPlanner:
    """Runs the whole catalog; a failed Deploy stage looks at availability first."""

    def plan(self, event: IncidentCreatedEvent) -> list[QuerySpec]:
        specs = list(CATALOG)
        if event.failed_stage == "Deploy":
            specs.sort(key=lambda s: s is not AVAILABILITY)
        return specs
