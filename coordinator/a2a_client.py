"""The investigation agents' side of A2A: send an evidence-ready notification.

**Owner:** P2 (live A2A slice). An agent's ``notifier`` is an ``async (Message) -> None``;
``notifier_from_env`` returns one that posts to the Coordinator service named by
``COORDINATOR_URL``, or ``None`` when it is unset so local runs and tests stay unchanged.
Delivery is best effort - the agents already swallow and log a notifier failure, because
the evidence is on the blackboard whether or not the notification arrives.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from a2a.client import ClientConfig, create_client
from a2a.types import Message
from a2a.types import a2a_pb2 as pb

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


class A2ANotifier:
    """Send one message to the Coordinator; the client is created lazily and reused."""

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._url = url
        self._timeout = timeout
        self._client: Any | None = None

    async def __call__(self, message: Message) -> None:
        try:
            await asyncio.wait_for(self._send(message), self._timeout)
        except Exception:
            self._client = None  # reconnect (and re-read the agent card) next time
            raise

    async def _send(self, message: Message) -> None:
        if self._client is None:
            self._client = await create_client(self._url, ClientConfig(streaming=False))
        async for _ in self._client.send_message(pb.SendMessageRequest(message=message)):
            pass


def notifier_from_env() -> Callable[[Message], Awaitable[None]] | None:
    url = os.environ.get("COORDINATOR_URL", "").strip()
    if not url:
        logger.info("COORDINATOR_URL not set; A2A notifications are disabled")
        return None
    return A2ANotifier(url)


__all__ = ["A2ANotifier", "notifier_from_env"]
