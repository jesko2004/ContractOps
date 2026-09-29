from __future__ import annotations

import json
import logging
import threading
from contextlib import AbstractContextManager
from random import getrandbits
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
    start_http_server,
)

from contractops.context import try_get_request_context

HTTP_REQUESTS = Counter(
    "contractops_http_requests_total",
    "HTTP requests handled by ContractOps",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "contractops_http_request_duration_seconds",
    "ContractOps HTTP request duration",
    ("method", "route"),
)
WORKER_OPERATIONS = Counter(
    "contractops_worker_operations_total",
    "Reliable event worker operations",
    ("operation", "outcome"),
)
SCHEDULER_OPERATIONS = Counter(
    "contractops_scheduler_operations_total",
    "Obligation scheduler operations",
    ("outcome",),
)
SECURITY_EVENTS = Counter(
    "contractops_security_events_total",
    "Authentication and authorization failures",
    ("status",),
)

_configure_lock = threading.Lock()
_configured = False


def configure_tracing(*, service_name: str, endpoint: str | None) -> None:
    global _configured
    with _configure_lock:
        if _configured:
            return
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if endpoint:
            parts = urlsplit(endpoint)
            path = parts.path.rstrip("/")
            if not path.endswith("/v1/traces"):
                path += "/v1/traces"
            trace_endpoint = urlunsplit(parts._replace(path=path))
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=trace_endpoint))
            )
        trace.set_tracer_provider(provider)
        _configured = True


def trace_id_from_span(span: trace.Span) -> str:
    context = span.get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else "0" * 32


def context_for_trace_id(trace_id: str | None) -> otel_context.Context | None:
    if not trace_id:
        return None
    try:
        value = int(trace_id, 16)
    except ValueError:
        return None
    if value == 0:
        return None
    span_context = SpanContext(
        trace_id=value,
        span_id=getrandbits(64) or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


def start_worker_span(
    name: str,
    *,
    trace_id: str | None,
    attributes: dict[str, str] | None = None,
) -> AbstractContextManager[trace.Span]:
    return trace.get_tracer("contractops.worker").start_as_current_span(
        name,
        context=context_for_trace_id(trace_id),
        attributes=attributes,
    )


class JsonFormatter(logging.Formatter):
    """Allowlist-only JSON logs; request bodies, headers, tokens and payloads are never read."""

    def format(self, record: logging.LogRecord) -> str:
        request = try_get_request_context()
        value: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request is not None:
            value["request_id"] = request.request_id
            value["trace_id"] = request.trace_id
        for key in (
            "event_name",
            "request_id",
            "trace_id",
            "tenant_id",
            "resource_type",
            "resource_id",
            "outcome",
            "http_method",
            "http_route",
            "http_status",
            "duration_ms",
        ):
            item = getattr(record, key, None)
            if item is not None:
                value[key] = item
        if record.exc_info and record.exc_info[0] is not None:
            value["exception_type"] = record.exc_info[0].__name__
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def observe_http(method: str, route: str, status: int, started_at: float) -> None:
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_DURATION.labels(method=method, route=route).observe(perf_counter() - started_at)


def prometheus_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def start_metrics_server(port: int) -> None:
    start_http_server(port)
