"""Keep a RabbitMQ connection alive for the whole life of the API.

At startup the API used to try to connect the publisher and the notification consumer **once**, in the background, with
five attempts (about 22 seconds). After a Docker restart RabbitMQ is often slower than that; the consumer then never
connected again (approval notifications silently stopped) and the health endpoint stayed red until someone restarted the
API. This supervisor keeps trying, with capped backoff, and re-checks a live connection now and then so that a dropped one
is rebuilt too.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

logger = logging.getLogger(__name__)


async def keep_connected(
    name: str,
    connect: Callable[[], Awaitable[bool]],
    is_connected: Callable[[], Awaitable[bool]],
    *,
    initial_delay: float = 2.0,
    max_delay: float = 30.0,
    idle_delay: float = 15.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run until cancelled: connect when not connected, back off while RabbitMQ is unreachable, re-check when connected."""
    delay = initial_delay
    while True:
        try:
            if await is_connected():
                delay = initial_delay
                await sleep(idle_delay)
                continue
            if await connect():
                logger.info("%s is connected to RabbitMQ", name)
                delay = initial_delay
                await sleep(idle_delay)
                continue
            logger.warning("%s cannot reach RabbitMQ yet; trying again in %.0fs", name, delay)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a supervisor that dies is the bug it exists to prevent
            logger.warning("%s: RabbitMQ connection check failed (%s); trying again in %.0fs", name, type(error).__name__, delay)
        await sleep(delay)
        delay = min(delay * 2, max_delay)
