"""Behavior tests for the sample orders service."""

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from target_app.app.config import Settings
from target_app.app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(Settings())) as test_client:
        yield test_client


def test_health_reports_service_ready(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "not_configured"}


def test_create_and_list_orders(client: TestClient) -> None:
    created = client.post("/orders", json={"item": "  widget ", "quantity": 3})

    assert created.status_code == 201
    order = created.json()
    assert order["item"] == "widget"
    assert order["quantity"] == 3

    listed = client.get("/orders")
    assert listed.status_code == 200
    assert [entry["id"] for entry in listed.json()] == [order["id"]]


def test_get_order_by_id(client: TestClient) -> None:
    created = client.post("/orders", json={"item": "book", "quantity": 1})

    response = client.get(f"/orders/{created.json()['id']}")

    assert response.status_code == 200
    assert response.json()["item"] == "book"


def test_missing_order_returns_404(client: TestClient) -> None:
    response = client.get("/orders/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


def test_invalid_order_is_rejected(client: TestClient) -> None:
    response = client.post("/orders", json={"item": "  ", "quantity": 0})

    assert response.status_code == 422


def test_fault_injection_is_disabled_by_default(client: TestClient) -> None:
    assert client.get("/slow?delay=0").status_code == 404
    assert client.get("/cpu?seconds=0").status_code == 404
    assert client.get("/leak").status_code == 404


@pytest.mark.asyncio
async def test_slow_endpoint_can_be_cancelled_by_client_timeout() -> None:
    application = create_app(Settings(enable_slow=True, slow_max_delay_seconds=1))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(async_client.get("/slow?delay=0.5"), timeout=0.02)


def test_metrics_include_requests_and_process_gauges(client: TestClient) -> None:
    client.get("/health")
    response = client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    assert "http_request_duration_seconds_bucket" in body
    assert 'http_requests_total{status="200"}' in body
    assert "process_cpu_seconds " in body
    assert "process_memory_bytes " in body


def test_enabled_leak_is_bounded() -> None:
    settings = Settings(enable_leak=True, leak_chunk_bytes=8, leak_max_bytes=8)
    with TestClient(create_app(settings)) as test_client:
        assert test_client.get("/leak").json() == {"allocated_bytes": 8}
        assert test_client.get("/leak").status_code == 413