"""OpenTelemetry tracing setup and W3C traceparent propagation for JenkinsGuardian."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from common.redaction import redact

logger = logging.getLogger(__name__)

_TRACER_INITIALIZED = False


def setup_tracing(service_name: str = "jenkins-guardian") -> trace.Tracer:
    """Install the process-wide tracer provider once; later calls just return a tracer.

    Spans are exported over OTLP when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set and printed to
    the console when ``OTEL_TRACES_CONSOLE=1``. With neither, spans still exist (so trace
    ids propagate and appear on blackboard entries) but are not exported anywhere.
    """
    global _TRACER_INITIALIZED
    if _TRACER_INITIALIZED:
        return trace.get_tracer(service_name)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            otlp_exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            logger.info("OTLP tracing initialized to %s for %s", redact(endpoint), service_name)
        except Exception as error:
            logger.warning(
                "Failed to configure OTLP exporter (%s)",
                type(error).__name__,
            )
    if os.getenv("OTEL_TRACES_CONSOLE", "0") == "1":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _TRACER_INITIALIZED = True
    return trace.get_tracer(service_name)


def get_tracer(service_name: str = "jenkins-guardian") -> trace.Tracer:
    """Get active tracer."""
    return trace.get_tracer(service_name)


def inject_traceparent(carrier: dict[str, str]) -> dict[str, str]:
    """Inject current span context as W3C traceparent header into carrier dict."""
    TraceContextTextMapPropagator().inject(carrier)
    return carrier


def extract_traceparent(carrier: Mapping[str, str]) -> trace.Context:
    """Extract W3C traceparent header from carrier dict and return span context."""
    return TraceContextTextMapPropagator().extract(carrier)


def current_traceparent_headers() -> dict[str, str]:
    """Headers that carry the active span to the next service; empty when there is none."""
    return inject_traceparent({})


def context_from_headers(headers: Mapping[str, Any] | None) -> trace.Context:
    """Trace context from HTTP or AMQP headers (names are case-insensitive, values may be bytes)."""
    carrier: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "ignore")
        carrier[str(name).lower()] = str(value)
    return extract_traceparent(carrier)


@contextmanager
def span_from_headers(
    tracer_name: str,
    span_name: str,
    headers: Mapping[str, Any] | None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[trace.Span]:
    """Run a block inside a span that continues the trace carried by a message's headers."""
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(
        span_name, context=context_from_headers(headers), attributes=dict(attributes or {})
    ) as span:
        yield span


def get_current_trace_and_span_ids() -> tuple[str | None, str | None]:
    """Return hex strings of current active (trace_id, span_id) or (None, None)."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    return None, None
