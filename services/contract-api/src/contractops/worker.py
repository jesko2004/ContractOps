from __future__ import annotations

import argparse
import logging
import socket
import time
from collections.abc import Sequence

from contractops.application.events import (
    NotificationAdapter,
    NotificationConsumer,
    OutboxPublisher,
)
from contractops.config import get_settings
from contractops.infrastructure.notifications import (
    LoggingNotificationAdapter,
    WebhookNotificationAdapter,
)
from contractops.infrastructure.postgres import PostgresWorkerEventStore, WorkerDatabase
from contractops.infrastructure.redis_streams import RedisEventStream


def _adapters(webhook_url: str | None, webhook_secret: str | None) -> Sequence[NotificationAdapter]:
    values: list[NotificationAdapter] = [LoggingNotificationAdapter()]
    if webhook_url:
        values.append(WebhookNotificationAdapter(webhook_url, secret=webhook_secret))
    return tuple(values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ContractOps reliable event worker")
    parser.add_argument("--once", action="store_true", help="process one batch and stop")
    arguments = parser.parse_args()
    settings = get_settings()
    if not settings.worker_database_url:
        raise SystemExit("CONTRACTOPS_WORKER_DATABASE_URL is required")
    logging.basicConfig(level=settings.log_level)
    worker_id = settings.event_consumer_name or socket.gethostname()
    database = WorkerDatabase(settings.worker_database_url)
    stream = RedisEventStream(
        settings.redis_url,
        stream_name=settings.event_stream_name,
        group_name=settings.event_consumer_group,
    )
    store = PostgresWorkerEventStore(database)
    publisher = OutboxPublisher(
        store,
        stream,
        worker_id=f"{worker_id}:publisher",
        batch_size=settings.event_batch_size,
        lease_seconds=settings.event_lease_seconds,
    )
    consumer = NotificationConsumer(
        store,
        stream,
        _adapters(settings.notification_webhook_url, settings.notification_webhook_secret),
        consumer_name=worker_id,
        batch_size=settings.event_batch_size,
        lease_seconds=settings.event_lease_seconds,
        claim_idle_ms=settings.event_claim_idle_ms,
    )
    try:
        while True:
            published = publisher.run_once()
            consumed = consumer.run_once(block_ms=100 if arguments.once else 1_000)
            if arguments.once:
                break
            if published == 0 and consumed == 0:
                time.sleep(settings.event_poll_interval_seconds)
    finally:
        stream.close()
        database.dispose()


if __name__ == "__main__":
    main()
