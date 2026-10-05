"""RabbitMQ consumer for the Metrics agent (Week 1 skeleton: receive and log only).

Investigation logic (PromQL selection, anomaly detection, evidence) arrives in Week 2.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

import aio_pika
from aio_pika.exceptions import AMQPConnectionError
from pydantic import ValidationError

from .config import MetricsAgentSettings, load_settings
from .investigator import InvestigationConfig, Investigator
from .models import IncidentCreatedEvent
from .planner import CatalogPlanner
from .prometheus_tool import PrometheusTool
from .store import DEFAULT_SCHEMA_PATH, LocalJsonEvidenceWriter

logger = logging.getLogger(__name__)

T = TypeVar("T")

_MAX_BACKOFF_SECONDS = 30.0
_CONNECT_ERRORS = (OSError, AMQPConnectionError)


async def connect_with_retry(
    connect: Callable[[], Awaitable[T]],
    retries: int,
    backoff_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call `connect`, retrying connection errors with exponential backoff.

    Makes at most `retries + 1` attempts, then re-raises the last error so a real
    configuration or network problem is not hidden by endless retrying.
    """
    for attempt in range(retries + 1):
        try:
            return await connect()
        except _CONNECT_ERRORS as exc:
            if attempt == retries:
                logger.error("rabbitmq unreachable, giving up", extra={"attempts": attempt + 1})
                raise
            delay = min(backoff_seconds * (2**attempt), _MAX_BACKOFF_SECONDS)
            logger.warning(
                "rabbitmq connection failed, retrying",
                extra={"attempt": attempt + 1, "retry_in_seconds": delay, "error": str(exc)},
            )
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class MetricsAgent:
    """Receives incident events and hands them to the Investigator."""

    def __init__(self, investigator: Investigator) -> None:
        self.investigator = investigator

    def handle_incident(self, event: IncidentCreatedEvent) -> None:
        logger.info("investigating incident", extra={"incident_id": str(event.incident_id)})
        self.investigator.investigate(event)


def build_agent(settings: MetricsAgentSettings, tool: PrometheusTool) -> MetricsAgent:
    schema_path = Path(settings.EVIDENCE_SCHEMA_PATH) if settings.EVIDENCE_SCHEMA_PATH else DEFAULT_SCHEMA_PATH
    config = InvestigationConfig(
        lookback=timedelta(seconds=settings.INVESTIGATION_LOOKBACK_SECONDS),
        tail=timedelta(seconds=settings.INVESTIGATION_TAIL_SECONDS),
        settle=timedelta(seconds=settings.INVESTIGATION_SETTLE_SECONDS),
        baseline=timedelta(seconds=settings.BASELINE_SECONDS),
        step=settings.QUERY_STEP,
    )
    writer = LocalJsonEvidenceWriter(Path(settings.EVIDENCE_DIR), schema_path)
    return MetricsAgent(Investigator(tool, CatalogPlanner(), writer, config))


def parse_event(body: bytes) -> IncidentCreatedEvent:
    """Validate a raw message body; raises ValueError/ValidationError on bad input."""
    return IncidentCreatedEvent.model_validate(json.loads(body))


async def run(settings: MetricsAgentSettings) -> None:
    tool = PrometheusTool(
        settings.PROMETHEUS_URL,
        timeout_seconds=settings.PROMETHEUS_TIMEOUT_SECONDS,
        max_retries=settings.PROMETHEUS_MAX_RETRIES,
        backoff_seconds=settings.PROMETHEUS_BACKOFF_SECONDS,
    )
    agent = build_agent(settings, tool)
    try:
        connection = await connect_with_retry(
            lambda: aio_pika.connect_robust(settings.RABBITMQ_URL),
            retries=settings.RABBITMQ_CONNECT_RETRIES,
            backoff_seconds=settings.RABBITMQ_CONNECT_BACKOFF_SECONDS,
        )
        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            exchange = await channel.declare_exchange(
                settings.EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
            )
            queue = await channel.declare_queue(settings.QUEUE_NAME, durable=True)
            await queue.bind(exchange, routing_key=settings.ROUTING_KEY)
            logger.info("metrics agent consuming", extra={"queue": settings.QUEUE_NAME})
            async with queue.iterator() as messages:
                async for message in messages:
                    # requeue=False: a malformed message must not loop forever.
                    async with message.process(requeue=False):
                        try:
                            event = parse_event(message.body)
                        except (ValueError, ValidationError):
                            logger.exception("dropping malformed incident message")
                            continue
                        try:
                            # Investigation does blocking HTTP; keep the event loop (heartbeats) free.
                            await asyncio.to_thread(agent.handle_incident, event)
                        except Exception:  # the consumer must outlive any single bad incident
                            logger.exception(
                                "investigation failed", extra={"incident_id": str(event.incident_id)}
                            )
    finally:
        tool.close()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
