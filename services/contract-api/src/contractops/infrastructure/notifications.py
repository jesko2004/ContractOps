from __future__ import annotations

import hashlib
import hmac
import json
import logging
from urllib.request import Request, urlopen

from contractops.domain.events import OutboxEvent

LOGGER = logging.getLogger("contractops.notifications")


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

    def __init__(self, url: str, *, secret: str | None = None, timeout_seconds: float = 5) -> None:
        if not url.startswith(("https://", "http://")):
            raise ValueError("webhook URL must use http or https")
        self._url = url
        self._secret = secret
        self._timeout_seconds = timeout_seconds

    @property
    def destination(self) -> str:
        digest = hashlib.sha256(self._url.encode("utf-8")).hexdigest()[:16]
        return f"webhook:{digest}"

    def send(self, event: OutboxEvent, *, idempotency_key: str) -> None:
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
        with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
            if not 200 <= response.status < 300:
                raise RuntimeError(f"webhook returned HTTP {response.status}")
