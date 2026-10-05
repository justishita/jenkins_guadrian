from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agents.metrics_agent.prometheus_tool import (
    PrometheusQueryError,
    PrometheusResponseError,
    PrometheusTool,
    PrometheusUnavailableError,
)

NOW = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)


def make_tool(handler, max_retries: int = 2) -> tuple[PrometheusTool, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(handler))
    tool = PrometheusTool(
        "http://prom", max_retries=max_retries, backoff_seconds=0.5, client=client, sleep=sleeps.append
    )
    return tool, sleeps


def vector_response(result: list) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})


def test_instant_query_parses_vector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query"
        assert request.url.params["query"] == "up"
        return vector_response([{"metric": {"job": "target_app"}, "value": [1759656600, "1"]}])

    tool, _ = make_tool(handler)
    result = tool.query("up")
    assert not result.empty
    assert result.series[0].labels == {"job": "target_app"}
    assert result.series[0].samples[0].value == 1.0
    assert result.series[0].samples[0].timestamp.tzinfo is not None


def test_range_query_parses_matrix_and_sends_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query_range"
        assert request.url.params["step"] == "15s"
        assert float(request.url.params["end"]) - float(request.url.params["start"]) == 300
        body = {
            "status": "success",
            "data": {"resultType": "matrix", "result": [{"metric": {}, "values": [[1, "2"], [16, "3"]]}]},
        }
        return httpx.Response(200, json=body)

    tool, _ = make_tool(handler)
    result = tool.query_range("rate(x[5m])", NOW - timedelta(minutes=5), NOW, "15s")
    assert [s.value for s in result.series[0].samples] == [2.0, 3.0]


def test_empty_result_is_not_an_error() -> None:
    tool, _ = make_tool(lambda r: vector_response([]))
    result = tool.query("nonexistent_metric")
    assert result.empty


def test_bad_promql_raises_and_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse error"})

    tool, sleeps = make_tool(handler)
    with pytest.raises(PrometheusQueryError, match="parse error"):
        tool.query("sum(")
    assert len(calls) == 1
    assert sleeps == []


def test_transient_5xx_retries_with_backoff_then_succeeds() -> None:
    responses = [httpx.Response(503), httpx.Response(503)]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0) if responses else vector_response([])

    tool, sleeps = make_tool(handler)
    assert tool.query("up").empty
    assert sleeps == [0.5, 1.0]


def test_retries_exhausted_raises_unavailable() -> None:
    tool, sleeps = make_tool(lambda r: httpx.Response(503), max_retries=2)
    with pytest.raises(PrometheusUnavailableError):
        tool.query("up")
    assert len(sleeps) == 2


def test_timeout_and_connection_errors_are_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    tool, sleeps = make_tool(handler, max_retries=1)
    with pytest.raises(PrometheusUnavailableError):
        tool.query("up")
    assert len(sleeps) == 1


def test_non_json_response_raises_response_error() -> None:
    tool, _ = make_tool(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(PrometheusResponseError):
        tool.query("up")


def test_malformed_payload_raises_response_error() -> None:
    body = {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}}]}}
    tool, _ = make_tool(lambda r: httpx.Response(200, json=body))
    with pytest.raises(PrometheusResponseError):
        tool.query("up")


def test_naive_datetime_rejected() -> None:
    tool, _ = make_tool(lambda r: vector_response([]))
    with pytest.raises(ValueError):
        tool.query("up", at=datetime(2026, 10, 5, tzinfo=None))  # noqa: DTZ001


def test_range_end_before_start_rejected() -> None:
    tool, _ = make_tool(lambda r: vector_response([]))
    with pytest.raises(PrometheusQueryError):
        tool.query_range("up", NOW, NOW - timedelta(minutes=1), "15s")
