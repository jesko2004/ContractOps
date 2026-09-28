from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from contractops.domain.events import OutboxEvent

LOGGER = logging.getLogger("contractops.notifications")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _validate_destination(url: str, *, allow_private_networks: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("webhook URL must use http or https and include a host")
    if parsed.username or parsed.password:
        raise ValueError("webhook URL must not include credentials")
    if allow_private_networks:
        return
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or default_port)
        }
    except socket.gaierror as error:
        raise RuntimeError("webhook host could not be resolved") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("webhook URL must resolve only to public network addresses")


def _envelope(event: OutboxEvent) -> dict[str, object]:
    return {
        "id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "type": event.event_type,
        "schema_version": event.schema_version,
        "aggregate": {
            "type": event.aggregate_type,
            "id": str(event.aggregate_id),
        },
        "payload": event.payload,
        "request_id": event.request_id,
        "trace_id": event.trace_id,
        "occurred_at": event.occurred_at.isoformat(),
    }


class LoggingNotificationAdapter:
    channel = "LOG"
    destination = "application-log"

    def send(self, event: OutboxEvent, *, idempotency_key: str) -> None:
        LOGGER.info(
            "contract event notification",
            extra={
                "event_id": str(event.id),
                "tenant_id": str(event.tenant_id),
                "event_type": event.event_type,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": str(event.aggregate_id),
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "idempotency_key": idempotency_key,
            },
        )


class WebhookNotificationAdapter:
    channel = "WEBHOOK"

    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        timeout_seconds: float = 5,
        allow_private_networks: bool = False,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("webhook URL must use http or https and include a host")
        if parsed.username or parsed.password:
            raise ValueError("webhook URL must not include credentials")
        self._url = url
        self._secret = secret
        self._timeout_seconds = timeout_seconds
        self._allow_private_networks = allow_private_networks

    @property
    def destination(self) -> str:
        digest = hashlib.sha256(self._url.encode("utf-8")).hexdigest()[:16]
        return f"webhook:{digest}"

    def send(self, event: OutboxEvent, *, idempotency_key: str) -> None:
        _validate_destination(
            self._url,
            allow_private_networks=self._allow_private_networks,
        )
        body = json.dumps(_envelope(event), separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "ContractOps-Webhook/1.0",
        }
        if self._secret:
            headers["X-ContractOps-Signature"] = hmac.new(
                self._secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
        request = Request(self._url, data=body, headers=headers, method="POST")
        opener = build_opener(_RejectRedirects())
        try:
            with opener.open(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"webhook returned HTTP {response.status}")
        except HTTPError as error:
            if 300 <= error.code < 400:
                raise RuntimeError("webhook redirects are not allowed") from error
            raise
