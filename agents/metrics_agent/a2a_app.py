"""A2A server for the Metrics Investigation Agent (port 9102).

The Coordinator sends ``POST /tasks`` with an incident id and attempt; this module reads the
incident context from the blackboard (posted by the backend), runs the same
``MetricsAgent.run_event`` the RabbitMQ consumer uses, and mirrors the evidence onto the
blackboard so the shared A2A server (``common.a2a_server``) can return a reference to it.
The response carries a pointer only, never the findings.

Which path triggers an investigation is decided by ``TRIGGER_MODE`` (see ``__main__``): in
``a2a`` mode this server is the only trigger and the RabbitMQ consumer is not started, so an
incident is never investigated twice.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI

from common.a2a_server import create_a2a_app
from common.blackboard import BlackboardEntry, get_blackboard
from common.models import Evidence

from .config import load_settings
from .consumer import MetricsAgent, build_agent
from .evidence import AGENT_NAME
from .models import IncidentCreatedEvent
from .prometheus_tool import PrometheusTool

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9102
# One investigation includes the settle wait (default 30 s) and several Prometheus queries.
DEFAULT_TIMEOUT_SECONDS = 120.0


def agent_card(url: str) -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Investigates Prometheus metrics around a failure window and writes anomaly evidence.",
        "version": "1.0.0",
        "url": url,
        "skills": [
            {
                "name": "investigate_incident",
                "description": "Detects metric anomalies around the failure time and writes evidence to the Blackboard.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "incident_id": {"type": "string", "format": "uuid"},
                        "attempt": {"type": "integer", "default": 0, "minimum": 0},
                    },
                    "required": ["incident_id"],
                },
            }
        ],
        "capabilities": {"push": False, "streaming": False},
    }


def build_run_fn(agent: MetricsAgent, blackboard_getter: Callable[[], Any] | None = None) -> Callable[..., Evidence]:
    """Return the synchronous task function ``create_a2a_app`` calls in a worker thread."""

    def run(incident_id: UUID | str, attempt: int = 0) -> Evidence:
        identifier = UUID(str(incident_id))
        blackboard = (blackboard_getter or get_blackboard)()  # resolved per call so tests can patch it
        context = blackboard.read(identifier, section="context", key="incident_event")
        if context is None:
            raise ValueError(f"No incident context found on the Blackboard for incident {identifier}")
        event = IncidentCreatedEvent.model_validate({**context.payload, "incident_id": str(identifier)})

        # run_event audits the run, investigates, and writes the Evidence Store entry.
        evidence = asyncio.run(agent.run_event(event))

        blackboard.post(
            BlackboardEntry(
                incident_id=identifier,
                section="evidence",
                key=AGENT_NAME,
                author=AGENT_NAME,
                payload=evidence.model_dump(mode="json"),
            )
        )
        return evidence

    return run


def create_app(agent: MetricsAgent | None = None, *, url: str | None = None, timeout_seconds: float | None = None) -> FastAPI:
    """Build the FastAPI app. ``agent`` is injectable for tests; production builds it from settings."""
    if agent is None:
        settings = load_settings()
        agent = build_agent(
            settings,
            PrometheusTool(
                settings.PROMETHEUS_URL,
                timeout_seconds=settings.PROMETHEUS_TIMEOUT_SECONDS,
                max_retries=settings.PROMETHEUS_MAX_RETRIES,
                backoff_seconds=settings.PROMETHEUS_BACKOFF_SECONDS,
            ),
        )
    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    return create_a2a_app(
        agent_card_data=agent_card(url or os.getenv("METRICS_AGENT_URL", f"http://localhost:{port}")),
        run_fn=build_run_fn(agent),
        timeout_seconds=timeout_seconds or float(os.getenv("A2A_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
    )


def serve() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    Path(settings.EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", str(DEFAULT_PORT))))


if __name__ == "__main__":
    serve()
