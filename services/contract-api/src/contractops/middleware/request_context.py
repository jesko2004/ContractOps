from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from contractops.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)


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
        scope.setdefault("state", {})["request_id"] = request_id
        token = bind_request_context(RequestContext(request_id=request_id))

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_context(token)
