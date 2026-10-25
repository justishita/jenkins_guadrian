"""OpenTelemetry tracing setup and W3C traceparent propagation for JenkinsGuardian."""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from common.redaction import redact

logger = logging.getLogger(__name__)

_TRACER_INITIALIZED = False


def setup_tracing(service_name: str = "jenkins-guardian") -> trace.Tracer:
    """Initialize OpenTelemetry tracer provider with OTLP exporter if configured."""
    global _TRACER_INITIALIZED

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


def get_current_trace_and_span_ids() -> tuple[str | None, str | None]:
    """Return hex strings of current active (trace_id, span_id) or (None, None)."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    return None, None
