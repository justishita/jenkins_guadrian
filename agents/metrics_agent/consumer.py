"""RabbitMQ consumer for the Metrics agent (Week 1 skeleton: receive and log only).

Investigation logic (PromQL selection, anomaly detection, evidence) arrives in Week 2.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aio_pika
from aio_pika.exceptions import AMQPConnectionError
from pydantic import ValidationError

from .config import MetricsAgentSettings, load_settings
from .models import IncidentCreatedEvent
from .prometheus_tool import PrometheusTool

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
    """Holds the PrometheusTool and handles one incident event."""

    def __init__(self, prometheus: PrometheusTool) -> None:
        self.prometheus = prometheus

    def handle_incident(self, event: IncidentCreatedEvent) -> None:
        logger.info("ready for investigation", extra={"incident_id": str(event.incident_id)})


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
    agent = MetricsAgent(tool)
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
                            agent.handle_incident(parse_event(message.body))
                        except (ValueError, ValidationError):
                            logger.exception("dropping malformed incident message")
    finally:
        tool.close()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
