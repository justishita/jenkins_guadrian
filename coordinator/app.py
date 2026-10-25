"""RabbitMQ-to-A2A dispatcher for the Jenkins investigation agent."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
from typing import Any

import aio_pika
from fastapi import FastAPI, HTTPException, Request, status
import httpx
from pydantic import ValidationError

from common.evidence_store import FileEvidenceStore
from common.models import IncidentCreatedEvent
from coordinator.agent import CoordinatorAgent

logger = logging.getLogger(__name__)


class CoordinatorRuntime:
    """Own the incident queue and dispatch Jenkins work over A2A."""

    def __init__(self) -> None:
        self.rabbitmq_url = os.getenv("RABBITMQ_URL", "")
        self.a2a_token = os.getenv("A2A_SHARED_TOKEN", "")
        self.jenkins_agent_url = os.getenv(
            "JENKINS_AGENT_URL",
            "http://jenkins-agent:9101",
        ).rstrip("/")
        self.timeout_seconds = float(os.getenv("A2A_TIMEOUT_SECONDS", "70"))
        self.exchange_name = os.getenv("EXCHANGE_NAME", "incidents")
        self.queue_name = os.getenv("COORDINATOR_QUEUE_NAME", "coordinator.incident.created")
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.queue: aio_pika.abc.AbstractQueue | None = None
        self.consumer_tag: str | None = None
        self.client: httpx.AsyncClient | None = None
        self.adk_agent: CoordinatorAgent | None = None

    async def start(self) -> None:
        if not self.rabbitmq_url:
            raise RuntimeError("RABBITMQ_URL must be configured")
        if not self.a2a_token:
            raise RuntimeError("A2A_SHARED_TOKEN must be configured")

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            headers={"Authorization": f"Bearer {self.a2a_token}"},
        )
        self.adk_agent = CoordinatorAgent(
            FileEvidenceStore(Path(os.getenv("EVIDENCE_DIR", "data/evidence")))
        )
        self.connection = await self._connect_with_retry()
        channel = await self.connection.channel()
        await channel.set_qos(prefetch_count=1)
        exchange = await channel.declare_exchange(
            self.exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        self.queue = await channel.declare_queue(self.queue_name, durable=True)
        await self.queue.bind(exchange, routing_key="incident.created")
        self.consumer_tag = await self.queue.consume(self._handle_message)
        logger.info("Coordinator consuming incident.created events")

    async def _connect_with_retry(self) -> aio_pika.abc.AbstractRobustConnection:
        retries = 4
        for attempt in range(retries + 1):
            try:
                return await aio_pika.connect_robust(self.rabbitmq_url, timeout=10)
            except (OSError, asyncio.TimeoutError, aio_pika.exceptions.AMQPException) as error:
                if attempt == retries:
                    raise RuntimeError("Coordinator could not connect to RabbitMQ") from error
                delay_seconds = min(0.5 * (2**attempt), 4.0)
                logger.warning(
                    "Coordinator RabbitMQ connect failed (%s); retrying",
                    type(error).__name__,
                )
                await asyncio.sleep(delay_seconds)
        raise RuntimeError("Coordinator RabbitMQ connection attempts were exhausted")

    async def _handle_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        try:
            async with message.process(requeue=True):
                try:
                    event = IncidentCreatedEvent.model_validate(json.loads(message.body))
                except (ValueError, ValidationError) as error:
                    logger.error(
                        "Discarding invalid incident.created message (%s)",
                        type(error).__name__,
                    )
                    return
                await self.dispatch_jenkins_task(event)
        except Exception as error:
            logger.error(
                "Coordinator dispatch failed for incident message (%s)",
                type(error).__name__,
            )
            raise

    async def dispatch_jenkins_task(self, event: IncidentCreatedEvent) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("Coordinator HTTP client has not started")

        url = f"{self.jenkins_agent_url}/tasks"
        payload = {
            "incident_id": str(event.incident_id),
            "attempt": event.remediation_attempt,
        }
        for attempt in range(3):
            try:
                response = await self.client.post(url, json=payload)
                if response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
                        continue
                response.raise_for_status()
                result = response.json()
                if (
                    not isinstance(result, dict)
                    or result.get("status") != "completed"
                    or result.get("incident_id") != str(event.incident_id)
                    or result.get("attempt") != event.remediation_attempt
                    or not isinstance(result.get("blackboard_ref"), dict)
                ):
                    raise RuntimeError("Jenkins agent returned an invalid A2A response")

                if self.adk_agent is not None:
                    view = await self.adk_agent.collect(event.incident_id)
                    logger.info(
                        "Coordinator read agent evidence from Blackboard",
                        extra={
                            "incident_id": str(event.incident_id),
                            "reporting_agents": list(view.reporting_agents),
                        },
                    )
                return result
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 2:
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500 or attempt == 2:
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise RuntimeError("Jenkins A2A dispatch attempts were exhausted")

    async def stop(self) -> None:
        if self.queue is not None and self.consumer_tag is not None:
            await self.queue.cancel(self.consumer_tag)
        if self.client is not None:
            await self.client.aclose()
        if self.connection is not None and not self.connection.is_closed:
            await self.connection.close()


runtime = CoordinatorRuntime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await runtime.start()
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="JenkinsGuardian Coordinator", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    active_runtime: CoordinatorRuntime = request.app.state.runtime
    if active_runtime.connection is None or active_runtime.connection.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Coordinator RabbitMQ connection is unavailable",
        )
    return {"status": "ok"}


app.state.runtime = runtime


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "9100")))
