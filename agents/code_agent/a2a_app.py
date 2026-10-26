"""A2A server for the Code Investigation Agent (port 9103).

The Coordinator sends ``POST /tasks`` with an incident id and attempt. This module reads the
incident context from the blackboard (posted by the backend), runs the same
``CodeInvestigationAgent.run_event`` the RabbitMQ consumer uses, and mirrors the evidence onto
the blackboard so ``common.a2a_server`` can answer with a reference to it - never the findings.

``TRIGGER_MODE`` (see ``__main__``) decides who starts an investigation: in ``a2a`` mode this
server is the only trigger and the RabbitMQ consumer is not started, so an incident is never
investigated twice.

A fresh agent and GitHub client are built per task. The GitHub client is async and each task runs
in its own event loop inside the worker thread ``common.a2a_server`` provides, so a shared client
would end up attached to a closed loop on the second incident.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI

from common.a2a_server import create_a2a_app
from common.blackboard import BlackboardEntry, get_blackboard
from common.models import Evidence, IncidentCreatedEvent

from .agent import AGENT_NAME, CodeInvestigationAgent, build_agent
from .config import load_settings
from .tools.github_client import GitHubClient

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9103
# GitHub lookups plus the reasoning step; generous because the LLM call dominates.
DEFAULT_TIMEOUT_SECONDS = 120.0

AgentBuilder = Callable[[], tuple[CodeInvestigationAgent, GitHubClient]]


def agent_card(url: str) -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Investigates whether a recent code change explains a CI failure and writes evidence.",
        "version": "1.0.0",
        "url": url,
        "skills": [
            {
                "name": "investigate_incident",
                "description": "Inspects the commit behind a failed build and writes findings to the Blackboard.",
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


def _default_builder() -> tuple[CodeInvestigationAgent, GitHubClient]:
    return build_agent(load_settings())


def build_run_fn(
    builder: AgentBuilder = _default_builder, blackboard_getter: Callable[[], Any] | None = None
) -> Callable[..., Evidence]:
    """Return the synchronous task function ``create_a2a_app`` calls in a worker thread."""

    async def investigate(event: IncidentCreatedEvent) -> Evidence:
        agent, github = builder()
        try:
            return await agent.run_event(event)
        finally:
            await github.aclose()

    def run(incident_id: UUID | str, attempt: int = 0) -> Evidence:
        identifier = UUID(str(incident_id))
        blackboard = (blackboard_getter or get_blackboard)()  # resolved per call so tests can patch it
        context = blackboard.read(identifier, section="context", key="incident_event")
        if context is None:
            raise ValueError(f"No incident context found on the Blackboard for incident {identifier}")
        event = IncidentCreatedEvent.model_validate({**context.payload, "incident_id": str(identifier)})

        # run_event audits the run and writes the Evidence Store entry.
        evidence = asyncio.run(investigate(event))

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


def create_app(
    builder: AgentBuilder | None = None, *, url: str | None = None, timeout_seconds: float | None = None
) -> FastAPI:
    """Build the FastAPI app. ``builder`` is injectable for tests; production builds from settings."""
    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    return create_a2a_app(
        agent_card_data=agent_card(url or os.getenv("CODE_AGENT_URL", f"http://localhost:{port}")),
        run_fn=build_run_fn(builder or _default_builder),
        timeout_seconds=timeout_seconds or float(os.getenv("A2A_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
    )


def serve() -> None:
    settings = load_settings()  # fail fast on a missing token or repository, like the consumer does
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", str(DEFAULT_PORT))))


if __name__ == "__main__":
    serve()
