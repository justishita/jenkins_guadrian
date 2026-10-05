"""Order storage backed by PostgreSQL when a dependency URL is configured."""

import asyncio
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc
from uuid import UUID, uuid4

import asyncpg

from .config import Settings


class DatabaseClient:
    """Use PostgreSQL when configured, otherwise keep orders in process memory."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        self._orders: dict[UUID, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._settings.database_url is None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._settings.database_url,
            timeout=self._settings.database_timeout_seconds,
            command_timeout=self._settings.database_timeout_seconds,
            min_size=1,
            max_size=5,
        )
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS service_orders (
                id UUID PRIMARY KEY,
                item TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def healthy(self) -> bool:
        if self._pool is None:
            return self._settings.database_url is None
        async with self._pool.acquire() as connection:
            result = await asyncio.wait_for(
                connection.fetchval("SELECT 1"),
                timeout=self._settings.database_timeout_seconds,
            )
        return result == 1

    async def create_order(self, item: str, quantity: int) -> dict[str, Any]:
        order_id = uuid4()
        if self._pool is not None:
            record = await self._pool.fetchrow(
                """
                INSERT INTO service_orders (id, item, quantity)
                VALUES ($1, $2, $3)
                RETURNING id, item, quantity, created_at
                """,
                order_id,
                item,
                quantity,
            )
            return dict(record)

        order = {
            "id": order_id,
            "item": item,
            "quantity": quantity,
            "created_at": datetime.now(UTC),
        }
        async with self._lock:
            self._orders[order_id] = order
        return order

    async def get_order(self, order_id: UUID) -> dict[str, Any] | None:
        if self._pool is not None:
            record = await self._pool.fetchrow(
                "SELECT id, item, quantity, created_at FROM service_orders WHERE id = $1",
                order_id,
            )
            return dict(record) if record is not None else None

        async with self._lock:
            return self._orders.get(order_id)

    async def list_orders(self) -> list[dict[str, Any]]:
        if self._pool is not None:
            records = await self._pool.fetch(
                "SELECT id, item, quantity, created_at FROM service_orders ORDER BY created_at, id"
            )
            return [dict(record) for record in records]

        async with self._lock:
            return list(self._orders.values())