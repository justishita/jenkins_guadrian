"""The API keeps retrying RabbitMQ instead of giving up after one startup burst (found after a Docker restart)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.events import publisher as publisher_module
from backend.events.supervisor import keep_connected
from backend.notifications import consumer as consumer_module


class Stop(Exception):
    """Raised by the fake sleep to end the otherwise endless loop after enough rounds."""


def run_supervisor(connect, is_connected, rounds: int, **kwargs) -> list[float]:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= rounds:
            raise asyncio.CancelledError

    async def main() -> None:
        with pytest.raises(asyncio.CancelledError):
            await keep_connected("test", connect, is_connected, sleep=sleep, **kwargs)

    asyncio.run(main())
    return delays


def test_it_keeps_trying_until_rabbitmq_is_up_then_settles_into_checking() -> None:
    state = {"up": False, "calls": 0}

    async def connect() -> bool:
        state["calls"] += 1
        state["up"] = state["calls"] >= 4  # RabbitMQ becomes reachable on the 4th try
        return state["up"]

    async def is_connected() -> bool:
        return state["up"]

    delays = run_supervisor(connect, is_connected, rounds=6, initial_delay=1.0, max_delay=30.0, idle_delay=15.0)
    assert state["calls"] == 4
    assert delays[:3] == [1.0, 2.0, 4.0]  # backing off while it is down
    assert delays[3:] == [15.0, 15.0, 15.0]  # then just checking now and then


def test_the_backoff_is_capped() -> None:
    async def connect() -> bool:
        return False

    async def is_connected() -> bool:
        return False

    delays = run_supervisor(connect, is_connected, rounds=8, initial_delay=2.0, max_delay=10.0)
    assert delays == [2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0, 10.0]


def test_a_connection_that_drops_later_is_rebuilt_and_the_delay_starts_over() -> None:
    state = {"up": True, "rounds": 0}

    async def is_connected() -> bool:
        state["rounds"] += 1
        if state["rounds"] == 2:
            state["up"] = False  # RabbitMQ goes away
        return state["up"]

    async def connect() -> bool:
        state["up"] = True
        return True

    delays = run_supervisor(connect, is_connected, rounds=4, initial_delay=2.0, idle_delay=15.0)
    assert delays == [15.0, 15.0, 15.0, 15.0]  # always back to checking: the drop was repaired on the next round
    assert state["up"] is True


def test_an_error_in_the_check_or_the_connect_does_not_kill_the_supervisor() -> None:
    calls = {"n": 0}

    async def is_connected() -> bool:
        raise RuntimeError("boom")

    async def connect() -> bool:
        calls["n"] += 1
        return True

    delays = run_supervisor(connect, is_connected, rounds=3, initial_delay=1.0)
    assert delays == [1.0, 2.0, 4.0]  # it kept going and backing off


def test_cancellation_ends_the_supervisor_cleanly_at_shutdown() -> None:
    async def never_connected() -> bool:
        return False

    async def connect() -> bool:
        return False

    async def main() -> None:
        task = asyncio.create_task(keep_connected("t", connect, never_connected, initial_delay=60.0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


# ---- the connection objects themselves ------------------------------------------------------------------------


class FakeConnection:
    def __init__(self, channel_error: Exception | None = None) -> None:
        self.is_closed = False
        self.closed_calls = 0
        self._channel_error = channel_error

    async def channel(self, **kwargs):
        if self._channel_error:
            raise self._channel_error
        channel = MagicMock()
        channel.is_closed = False
        channel.set_qos = AsyncMock()
        channel.declare_exchange = AsyncMock(return_value=MagicMock(name="exchange"))
        queue = MagicMock()
        queue.bind = AsyncMock()
        queue.consume = AsyncMock(return_value="tag-1")
        channel.declare_queue = AsyncMock(return_value=queue)
        return channel

    async def close(self) -> None:
        self.is_closed = True
        self.closed_calls += 1


def test_the_publisher_drops_a_half_built_connection_so_it_is_not_mistaken_for_a_working_one(monkeypatch) -> None:
    broken = FakeConnection(channel_error=OSError("channel failed"))
    monkeypatch.setattr(publisher_module.aio_pika, "connect_robust", AsyncMock(return_value=broken))
    publisher = publisher_module.RabbitMQPublisher("amqp://x")
    assert asyncio.run(publisher.connect(max_retries=1, retry_interval=0.0)) is False
    assert publisher.connection is None and publisher.exchange is None and broken.closed_calls == 1
    assert asyncio.run(publisher.is_healthy()) is False  # the supervisor will try again


def test_the_publisher_connects_when_rabbitmq_is_there(monkeypatch) -> None:
    good = FakeConnection()
    monkeypatch.setattr(publisher_module.aio_pika, "connect_robust", AsyncMock(return_value=good))
    publisher = publisher_module.RabbitMQPublisher("amqp://x")
    assert asyncio.run(publisher.connect(max_retries=1, retry_interval=0.0)) is True
    assert asyncio.run(publisher.is_healthy()) is True and publisher.exchange is not None


def test_the_consumer_reports_connected_only_while_it_is_actually_consuming(monkeypatch) -> None:
    consumer = consumer_module.ApprovalRequestedConsumer("amqp://x")
    assert asyncio.run(consumer.is_connected()) is False
    good = FakeConnection()
    monkeypatch.setattr(consumer_module.aio_pika, "connect_robust", AsyncMock(return_value=good))
    assert asyncio.run(consumer.connect(max_retries=1, retry_interval=0.0)) is True
    assert asyncio.run(consumer.is_connected()) is True and consumer.consumer_tag == "tag-1"
    good.is_closed = True  # the connection dropped
    assert asyncio.run(consumer.is_connected()) is False


def test_a_consumer_that_failed_while_declaring_its_queue_is_reset_not_left_half_open(monkeypatch) -> None:
    broken = FakeConnection(channel_error=OSError("declare failed"))
    monkeypatch.setattr(consumer_module.aio_pika, "connect_robust", AsyncMock(return_value=broken))
    consumer = consumer_module.ApprovalRequestedConsumer("amqp://x")
    assert asyncio.run(consumer.connect(max_retries=1, retry_interval=0.0)) is False
    assert consumer.connection is None and consumer.consumer_tag is None and broken.closed_calls == 1
    # the next attempt really connects instead of answering "already connected"
    good = FakeConnection()
    monkeypatch.setattr(consumer_module.aio_pika, "connect_robust", AsyncMock(return_value=good))
    assert asyncio.run(consumer.connect(max_retries=1, retry_interval=0.0)) is True
    assert asyncio.run(consumer.is_connected()) is True


def test_the_app_starts_and_stops_cleanly_even_when_rabbitmq_is_unreachable() -> None:
    """Startup no longer depends on RabbitMQ being ready: the supervisors retry in the background and are cancelled at shutdown."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        assert client.get("/health/live").status_code in (200, 503)
    # leaving the context ran the shutdown path: cancelling the supervisors must not raise or hang
