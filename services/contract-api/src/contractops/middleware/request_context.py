import logging
from time import perf_counter
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.propagate import extract
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from contractops.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from contractops.observability import SECURITY_EVENTS, observe_http, trace_id_from_span

logger = logging.getLogger("contractops.http")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Do not trust a client-supplied tenant id. Authentication will derive tenant
        # context server-side in M1. A valid upstream request id may be accepted later
        # once trusted proxy boundaries are configured.
        request_id = f"req_{uuid4().hex}"
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        carrier = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        tracer = trace.get_tracer("contractops.http")
        started_at = perf_counter()
        response_status = 500
        with tracer.start_as_current_span(
            f"{scope.get('method', 'HTTP')} {scope.get('path', '/')}",
            context=extract(carrier),
            kind=trace.SpanKind.SERVER,
            attributes={
                "http.request.method": scope.get("method", ""),
                "url.path": scope.get("path", ""),
            },
        ) as span:
            trace_id = trace_id_from_span(span)
            state["trace_id"] = trace_id
            token = bind_request_context(RequestContext(request_id=request_id, trace_id=trace_id))

            async def send_with_context(message: Message) -> None:
                nonlocal response_status
                if message["type"] == "http.response.start":
                    response_status = int(message["status"])
                    span.set_attribute("http.response.status_code", response_status)
                    headers = MutableHeaders(scope=message)
                    headers["X-Request-ID"] = request_id
                    headers["X-Trace-ID"] = trace_id
                await send(message)

            try:
                await self.app(scope, receive, send_with_context)
            finally:
                route_object = scope.get("route")
                route = getattr(route_object, "path", scope.get("path", "unknown"))
                method = str(scope.get("method", "HTTP"))
                observe_http(method, str(route), response_status, started_at)
                duration_ms = round((perf_counter() - started_at) * 1000, 3)
                logger.info(
                    "http request completed",
                    extra={
                        "event_name": "http.request.completed",
                        "request_id": request_id,
                        "trace_id": trace_id,
                        "http_method": method,
                        "http_route": route,
                        "http_status": response_status,
                        "duration_ms": duration_ms,
                    },
                )
                if response_status in {401, 403}:
                    SECURITY_EVENTS.labels(status=str(response_status)).inc()
                    logger.warning(
                        "security access denied",
                        extra={
                            "event_name": "security.access.denied",
                            "request_id": request_id,
                            "trace_id": trace_id,
                            "http_method": method,
                            "http_route": route,
                            "http_status": response_status,
                        },
                    )
                    actor = state.get("actor")
                    application = scope.get("app")
                    application_state = getattr(application, "state", None)
                    audit_recorder = getattr(application_state, "audit_recorder", None)
                    if actor is not None and audit_recorder is not None:
                        try:
                            audit_recorder.record_security(
                                actor,
                                request_id=request_id,
                                trace_id=trace_id,
                                method=method,
                                route=str(route),
                                status_code=response_status,
                            )
                        except Exception:
                            logger.exception(
                                "security audit persistence failed",
                                extra={
                                    "event_name": "security.audit.persistence_failed",
                                    "request_id": request_id,
                                    "trace_id": trace_id,
                                    "http_method": method,
                                    "http_route": route,
                                    "http_status": response_status,
                                },
                            )
                reset_request_context(token)
