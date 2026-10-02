"""FastAPI orders service with opt-in fault injection and Prometheus metrics."""

import asyncio
import hashlib
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator
from uuid import UUID

import psutil
from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import RequestResponseEndpoint

from .config import Settings, load_settings
from .db_client import DatabaseClient


class OrderCreate(BaseModel):
    item: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0, le=10_000)

    @field_validator("item")
    @classmethod
    def validate_item(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("item must not be blank")
        return value


class Order(BaseModel):
    id: UUID
    item: str
    quantity: int
    created_at: datetime


def create_app(settings: Settings | None = None) -> FastAPI:
    service_settings = settings or load_settings()
    database = DatabaseClient(service_settings)
    leaked_memory: list[bytearray] = []
    leaked_bytes = 0
    registry = CollectorRegistry()
    request_duration = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ("method", "path"),
        registry=registry,
    )
    request_count = Counter(
        "http_requests_total",
        "Total HTTP requests by response status",
        ("status",),
        registry=registry,
    )
    process_cpu = Gauge(
        "process_cpu_seconds",
        "Total user and system CPU time used by this process",
        registry=registry,
    )
    process_memory = Gauge(
        "process_memory_bytes",
        "Resident memory used by this process in bytes",
        registry=registry,
    )
    process = psutil.Process(os.getpid())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.connect()
        try:
            yield
        finally:
            await database.close()

    application = FastAPI(title="Orders Service", lifespan=lifespan)

    @application.middleware("http")
    async def record_request_metrics(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            request_duration.labels(request.method, path).observe(time.perf_counter() - started_at)
            request_count.labels(str(status_code)).inc()

    @application.get("/health")
    async def health() -> dict[str, str]:
        if not await database.healthy():
            raise HTTPException(status_code=503, detail="database dependency unavailable")
        database_status = "connected" if service_settings.database_url else "not_configured"
        return {"status": "ok", "database": database_status}

    @application.post("/orders", response_model=Order, status_code=201)
    async def create_order(order: OrderCreate) -> Order:
        stored_order = await database.create_order(order.item, order.quantity)
        return Order.model_validate(stored_order)

    @application.get("/orders", response_model=list[Order])
    async def list_orders() -> list[Order]:
        stored_orders = await database.list_orders()
        return [Order.model_validate(order) for order in stored_orders]

    @application.get("/orders/{order_id}", response_model=Order)
    async def get_order(order_id: UUID) -> Order:
        stored_order = await database.get_order(order_id)
        if stored_order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return Order.model_validate(stored_order)

    @application.get("/slow")
    async def slow(delay: float = Query(default=1.0, ge=0)) -> dict[str, float]:
        if not service_settings.enable_slow:
            raise HTTPException(status_code=404, detail="not found")
        if delay > service_settings.slow_max_delay_seconds:
            raise HTTPException(status_code=422, detail="delay exceeds configured maximum")
        await asyncio.sleep(delay)
        return {"slept_seconds": delay}

    @application.get("/cpu")
    def cpu(seconds: float = Query(default=1.0, ge=0)) -> dict[str, float]:
        if not service_settings.enable_cpu:
            raise HTTPException(status_code=404, detail="not found")
        if seconds > service_settings.cpu_max_seconds:
            raise HTTPException(status_code=422, detail="seconds exceeds configured maximum")
        deadline = time.perf_counter() + seconds
        value = b"orders-service-cpu"
        while time.perf_counter() < deadline:
            value = hashlib.sha256(value).digest()
        return {"burned_seconds": seconds}

    @application.get("/leak")
    def leak() -> dict[str, int]:
        nonlocal leaked_bytes
        if not service_settings.enable_leak:
            raise HTTPException(status_code=404, detail="not found")
        if leaked_bytes + service_settings.leak_chunk_bytes > service_settings.leak_max_bytes:
            raise HTTPException(status_code=413, detail="configured memory growth limit reached")
        leaked_memory.append(bytearray(service_settings.leak_chunk_bytes))
        leaked_bytes += service_settings.leak_chunk_bytes
        return {"allocated_bytes": leaked_bytes}

    @application.get("/metrics")
    def metrics() -> Response:
        cpu_times = process.cpu_times()
        process_cpu.set(cpu_times.user + cpu_times.system)
        process_memory.set(process.memory_info().rss)
        return Response(
            content=generate_latest(registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    return application


app = create_app()