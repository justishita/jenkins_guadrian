import asyncio
import json
import logging
from typing import Any, Dict, Optional
import aio_pika

from backend.config import settings

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
                except Exception as e:
                    logger.warning("RabbitMQ connection attempt %d failed: %s", attempt, e)
                    if attempt < max_retries:
                        await asyncio.sleep(retry_interval * (2 ** (attempt - 1)))
                    else:
                        logger.error("Failed to connect to RabbitMQ after %d attempts.", max_retries)
            return False

    async def publish_incident_created(self, payload: Dict[str, Any]) -> bool:
        """Publish an incident.created message to the topic exchange with persistent delivery."""
        try:
            if not self.exchange or (self.connection and self.connection.is_closed):
                connected = await self.connect(max_retries=3, retry_interval=1.0)
                if not connected or not self.exchange:
                    logger.error("Cannot publish message: RabbitMQ unavailable.")
                    return False

            message_body = json.dumps(payload).encode("utf-8")
            message = aio_pika.Message(
                body=message_body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type="application/json",
            )
            # Publishes to exchange with routing key incident.created and publisher confirmation
            await self.exchange.publish(message, routing_key="incident.created")
            logger.info("Published incident.created event for incident_id: %s", payload.get("incident_id"))
            return True
        except Exception as e:
            logger.error("Failed to publish incident.created message: %s", e)
            return False

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
            except Exception as e:
                logger.warning("Error closing RabbitMQ connection: %s", e)


publisher = RabbitMQPublisher()
