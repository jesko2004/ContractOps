from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Server-derived metadata that follows one request through the application."""

    request_id: str


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "contractops_request_context",
    default=None,
)


def bind_request_context(context: RequestContext) -> Token[RequestContext | None]:
    return _request_context.set(context)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _request_context.reset(token)


def get_request_context() -> RequestContext:
    context = _request_context.get()
    if context is None:
        raise RuntimeError("request context is not available outside an HTTP request")
    return context
