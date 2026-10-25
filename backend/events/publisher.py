import asyncio
import json
import logging
from typing import Any, Dict, Optional
import aio_pika

from backend.config import settings
from common.redaction import redact_value

logger = logging.getLogger(__name__)


class RabbitMQPublisher:
    """RabbitMQ Publisher with durable topic exchange, publisher confirms, and persistent messages."""

    def __init__(self, rabbitmq_url: Optional[str] = None):
        self.rabbitmq_url = rabbitmq_url or settings.RABBITMQ_URL
        self.connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
        self.channel: Optional[aio_pika.abc.AbstractRobustChannel] = None
        self.exchange: Optional[aio_pika.abc.AbstractExchange] = None
        self._lock = asyncio.Lock()
        self.exchange_name = "incidents"

    async def connect(self, max_retries: int = 5, retry_interval: float = 2.0) -> bool:
        """Establish connection with retries and exponential backoff."""
        async with self._lock:
            if self.connection and not self.connection.is_closed:
                return True

            for attempt in range(1, max_retries + 1):
                try:
                    logger.info("Attempting to connect to RabbitMQ (attempt %d/%d)...", attempt, max_retries)
                    self.connection = await aio_pika.connect_robust(self.rabbitmq_url)
                    self.channel = await self.connection.channel(publisher_confirms=True)
                    self.exchange = await self.channel.declare_exchange(
                        self.exchange_name,
                        type=aio_pika.ExchangeType.TOPIC,
                        durable=True,
                    )
                    logger.info("Successfully connected to RabbitMQ and declared exchange '%s'.", self.exchange_name)
                    return True
                except Exception as error:
                    logger.warning(
                        "RabbitMQ connection attempt %d failed (%s)",
                        attempt,
                        type(error).__name__,
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(
                            min(retry_interval * (2 ** (attempt - 1)), 8.0)
                        )
                    else:
                        logger.error("Failed to connect to RabbitMQ after %d attempts.", max_retries)
            return False

    async def publish_event(self, routing_key: str, payload: Dict[str, Any]) -> bool:
        """Publish a message to the incidents topic exchange with persistent delivery."""
        try:
            if not self.exchange or (self.connection and self.connection.is_closed):
                connected = await self.connect(max_retries=3, retry_interval=1.0)
                if not connected or not self.exchange:
                    logger.error("Cannot publish message: RabbitMQ unavailable.")
                    return False

            safe_payload = redact_value(payload)
            message_body = json.dumps(safe_payload).encode("utf-8")
            message = aio_pika.Message(
                body=message_body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type="application/json",
            )
            confirmed = await self.exchange.publish(message, routing_key=routing_key)
            if confirmed is False:
                logger.error("RabbitMQ did not confirm %s event delivery", routing_key)
                return False
            logger.info("Published %s event for incident_id: %s", routing_key, payload.get("incident_id"))
            return True
        except Exception as error:
            logger.error(
                "Failed to publish %s message (%s)",
                routing_key,
                type(error).__name__,
            )
            return False

    async def publish_incident_created(self, payload: Dict[str, Any]) -> bool:
        """Publish an incident.created message to the topic exchange with persistent delivery."""
        return await self.publish_event("incident.created", payload)

    async def publish_validation_completed(self, payload: Dict[str, Any]) -> bool:
        """Publish a validation.completed message to the incidents topic exchange."""
        return await self.publish_event("validation.completed", payload)

    async def publish_incident_reinvestigate(self, payload: Dict[str, Any]) -> bool:
        """Publish an incident.reinvestigate message to the incidents topic exchange."""
        return await self.publish_event("incident.reinvestigate", payload)

    async def publish_approval_decided(self, payload: Dict[str, Any]) -> bool:
        """Publish an approval.decided message to the incidents topic exchange."""
        return await self.publish_event("approval.decided", payload)

    async def is_healthy(self) -> bool:
        """Check if connection to RabbitMQ is currently alive."""
        if not self.connection or self.connection.is_closed:
            return False
        return True

    async def close(self) -> None:
        """Close connection cleanly."""
        async with self._lock:
            try:
                if self.channel and not self.channel.is_closed:
                    await self.channel.close()
                if self.connection and not self.connection.is_closed:
                    await self.connection.close()
                logger.info("RabbitMQ connection closed.")
            except Exception as error:
                logger.warning(
                    "Error closing RabbitMQ connection (%s)",
                    type(error).__name__,
                )


publisher = RabbitMQPublisher()
