"""A2A task server for CrewAI investigation agents."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
from typing import Any, Callable
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from common.blackboard import get_blackboard
from common.models import Evidence
from common.tracing import extract_traceparent, get_tracer

logger = logging.getLogger(__name__)

_EXECUTION_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
_TASK_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
_EXECUTION_TASKS: dict[tuple[str, int], asyncio.Task[Response]] = {}


class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    incident_id: UUID
    attempt: int = Field(default=0, ge=0)
    previous_failure_summary: str | None = None
    new_build_url: str | None = None


def verify_a2a_bearer_token(authorization: str | None = Header(None)) -> None:
    """Require a configured A2A token and compare it without timing leaks."""
    expected_token = os.getenv("A2A_SHARED_TOKEN")
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A2A_SHARED_TOKEN is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid A2A shared bearer token",
        )


def create_a2a_app(
    agent_card_data: dict[str, Any],
    run_fn: Callable[..., Evidence | Any],
    timeout_seconds: float = 60.0,
) -> FastAPI:
    """Serve authenticated, idempotent A2A tasks; return only a Blackboard reference."""
    app = FastAPI(title=agent_card_data.get("name", "A2A Agent"))

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")
    async def get_agent_card() -> dict[str, Any]:
        return agent_card_data

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/tasks")
    @app.post("/messages")
    async def handle_a2a_task(
        task: AgentTaskRequest,
        request: Request,
        _: None = Depends(verify_a2a_bearer_token),
    ) -> Response:
        key = (str(task.incident_id), task.attempt)
        task_lock = _TASK_LOCKS.setdefault(key, asyncio.Lock())
        async with task_lock:
            cached = _EXECUTION_CACHE.get(key)
            if cached is not None:
                return JSONResponse(status_code=status.HTTP_200_OK, content=cached)

            carrier = {name.lower(): value for name, value in request.headers.items()}
            trace_context = extract_traceparent(carrier)
            tracer = get_tracer(agent_card_data.get("name", "agent"))
            execution_task = _EXECUTION_TASKS.get(key)
            if execution_task is None:
                execution_task = asyncio.create_task(
                    _run_with_trace(
                        tracer=tracer,
                        trace_context=trace_context,
                        task=task,
                        key=key,
                        run_fn=run_fn,
                        agent_card_data=agent_card_data,
                    )
                )
                _EXECUTION_TASKS[key] = execution_task

        try:
            response = await asyncio.wait_for(
                asyncio.shield(execution_task),
                timeout=timeout_seconds,
            )
            async with task_lock:
                if _EXECUTION_TASKS.get(key) is execution_task:
                    _EXECUTION_TASKS.pop(key, None)
            return response
        except asyncio.TimeoutError:
            logger.error("A2A task timed out for incident %s", task.incident_id)
            return JSONResponse(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                content={
                    "status": "failed",
                    "reason": "task_timeout",
                    "incident_id": str(task.incident_id),
                },
            )

    return app


async def _execute_task(
    *,
    task: AgentTaskRequest,
    key: tuple[str, int],
    run_fn: Callable[..., Evidence | Any],
    agent_card_data: dict[str, Any],
) -> Response:
    try:
        def _call_run_fn():
            try:
                sig = inspect.signature(run_fn)
                params = list(sig.parameters.values())
                accepts_varargs = any(p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD) for p in params)
                if len(params) >= 4 or accepts_varargs:
                    return run_fn(
                        task.incident_id,
                        task.attempt,
                        task.previous_failure_summary,
                        task.new_build_url,
                    )
                elif len(params) == 2:
                    return run_fn(task.incident_id, task.attempt)
                elif len(params) == 1:
                    return run_fn(task.incident_id)
                else:
                    return run_fn(
                        task.incident_id,
                        task.attempt,
                        task.previous_failure_summary,
                        task.new_build_url,
                    )
            except TypeError:
                return run_fn(task.incident_id, task.attempt)

        await asyncio.to_thread(_call_run_fn)
        agent_name = str(agent_card_data.get("name", ""))
        entry = get_blackboard().read(
            task.incident_id,
            section="evidence",
            key=agent_name,
        )
        if entry is None:
            raise RuntimeError("Agent completed without writing evidence to the Blackboard")

        payload = {
            "status": "completed",
            "incident_id": str(task.incident_id),
            "attempt": task.attempt,
            "blackboard_ref": {
                "incident_id": str(task.incident_id),
                "section": "evidence",
                "key": agent_name,
                "version": entry.version,
            },
        }
        _EXECUTION_CACHE[key] = payload
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
    except Exception as error:
        logger.error(
            "A2A task failed for incident %s (%s)",
            task.incident_id,
            type(error).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "failed",
                "reason": type(error).__name__,
                "incident_id": str(task.incident_id),
            },
        )


async def _run_with_trace(
    *,
    tracer: Any,
    trace_context: Any,
    task: AgentTaskRequest,
    key: tuple[str, int],
    run_fn: Callable[..., Evidence | Any],
    agent_card_data: dict[str, Any],
) -> Response:
    with tracer.start_as_current_span("a2a_execute_task", context=trace_context):
        return await _execute_task(
            task=task,
            key=key,
            run_fn=run_fn,
            agent_card_data=agent_card_data,
        )
