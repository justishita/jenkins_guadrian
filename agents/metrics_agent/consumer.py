"""RabbitMQ consumer for the Metrics agent (Week 1 skeleton: receive and log only).

Investigation logic (PromQL selection, anomaly detection, evidence) arrives in Week 2.
"""

import asyncio
import json
import logging

import aio_pika
from pydantic import ValidationError

from .config import MetricsAgentSettings, load_settings
from .models import IncidentCreatedEvent
from .prometheus_tool import PrometheusTool

logger = logging.getLogger(__name__)


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
        connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
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
