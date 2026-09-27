from __future__ import annotations

import argparse
import socket
import time
from datetime import UTC, datetime

from contractops.application.obligations import ObligationScheduler
from contractops.config import get_settings
from contractops.infrastructure.postgres import (
    PostgresSchedulerObligationStore,
    WorkerDatabase,
)
from contractops.observability import configure_logging, configure_tracing, start_metrics_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ContractOps obligation scheduler")
    parser.add_argument("--once", action="store_true", help="process one due batch and stop")
    arguments = parser.parse_args()
    settings = get_settings()
    if not settings.worker_database_url:
        raise SystemExit("CONTRACTOPS_WORKER_DATABASE_URL is required")
    configure_logging(settings.log_level)
    configure_tracing(
        service_name=f"{settings.otel_service_name}-scheduler",
        endpoint=settings.otel_exporter_otlp_endpoint,
    )
    start_metrics_server(settings.scheduler_metrics_port)
    database = WorkerDatabase(settings.worker_database_url)
    scheduler = ObligationScheduler(
        PostgresSchedulerObligationStore(database),
        worker_id=f"{socket.gethostname()}:scheduler",
        batch_size=settings.obligation_batch_size,
        lease_seconds=settings.obligation_lease_seconds,
    )
    try:
        while True:
            processed = scheduler.run_once(datetime.now(UTC))
            if arguments.once:
                break
            if processed == 0:
                time.sleep(settings.obligation_poll_interval_seconds)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
