"""Standalone Prometheus HTTP API client.

Deliberately independent of the agent and consumer: the agent only calls
`query()` / `query_range()` and never touches HTTP.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import QueryResult, Sample, Series

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PrometheusError(Exception):
    """Base class for all PrometheusTool failures."""


class PrometheusUnavailableError(PrometheusError):
    """Transient failure (timeout, connection error, 5xx) that survived all retries."""


class PrometheusQueryError(PrometheusError):
    """Prometheus rejected the query (bad PromQL, bad parameters). Never retried."""


class PrometheusResponseError(PrometheusError):
    """Prometheus answered with something that is not a valid API response."""


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def _epoch(value: datetime) -> float:
    return _to_utc(value).timestamp()


def _parse_sample(raw: list[Any]) -> Sample:
    return Sample(timestamp=datetime.fromtimestamp(float(raw[0]), tz=timezone.utc), value=float(raw[1]))


class PrometheusTool:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 5.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds)
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def query(self, promql: str, at: datetime | None = None) -> QueryResult:
        """Instant query. `at` must be timezone-aware; defaults to the server's now."""
        params: dict[str, Any] = {"query": promql}
        if at is not None:
            params["time"] = _epoch(at)
        return self._run("/api/v1/query", params, promql)

    def query_range(self, promql: str, start: datetime, end: datetime, step: str) -> QueryResult:
        """Range query. `step` is a Prometheus duration (e.g. '15s') or seconds."""
        if _to_utc(end) <= _to_utc(start):
            raise PrometheusQueryError("end must be after start")
        params: dict[str, Any] = {"query": promql, "start": _epoch(start), "end": _epoch(end), "step": step}
        return self._run("/api/v1/query_range", params, promql)

    def _run(self, path: str, params: dict[str, Any], promql: str) -> QueryResult:
        return self._parse(self._get_with_retry(path, params), promql)

    def _get_with_retry(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("prometheus request failed", extra={"attempt": attempt + 1, "error": str(exc)})
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    return self._decode(response)
                last_error = PrometheusUnavailableError(f"HTTP {response.status_code}")
                logger.warning(
                    "prometheus transient status",
                    extra={"attempt": attempt + 1, "status": response.status_code},
                )
            if attempt < self._max_retries:
                self._sleep(self._backoff * (2**attempt))
        raise PrometheusUnavailableError(f"Prometheus unavailable after retries: {last_error}")

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise PrometheusResponseError(f"non-JSON response (HTTP {response.status_code})") from exc
        if not isinstance(body, dict):
            raise PrometheusResponseError("unexpected response shape")
        if response.status_code >= 400 or body.get("status") != "success":
            message = body.get("error", f"HTTP {response.status_code}")
            if 400 <= response.status_code < 500:
                raise PrometheusQueryError(f"{body.get('errorType', 'error')}: {message}")
            raise PrometheusResponseError(str(message))
        return body

    @staticmethod
    def _parse(body: dict[str, Any], promql: str) -> QueryResult:
        try:
            data = body["data"]
            result_type = data["resultType"]
            raw = data["result"]
            if result_type == "vector":
                series = [Series(labels=r["metric"], samples=[_parse_sample(r["value"])]) for r in raw]
            elif result_type == "matrix":
                series = [
                    Series(labels=r["metric"], samples=[_parse_sample(v) for v in r["values"]]) for r in raw
                ]
            elif result_type == "scalar":
                series = [Series(labels={}, samples=[_parse_sample(raw)])]
            elif result_type == "string":
                series = []
            else:
                raise PrometheusResponseError(f"unsupported resultType {result_type!r}")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PrometheusResponseError(f"malformed response: {exc}") from exc
        return QueryResult(promql=promql, result_type=result_type, series=series)
