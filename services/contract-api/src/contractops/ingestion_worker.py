from __future__ import annotations

import argparse
import hashlib
import socket
import time
from functools import partial

from contractops.config import get_settings
from contractops.errors import ContractOpsError
from contractops.infrastructure.document_parsing import extract_rule_findings, parse_document
from contractops.infrastructure.lease_heartbeat import maintain_lease
from contractops.infrastructure.model_suggestions import OpenAICompatibleFindingSuggester
from contractops.infrastructure.object_store import MinioObjectStore
from contractops.infrastructure.postgres.database import WorkerDatabase
from contractops.infrastructure.postgres.ingestion import PostgresIngestionStore
from contractops.observability import configure_logging, configure_tracing, start_metrics_server
from contractops.preflight import validate_runtime_role


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ContractOps document ingestion worker")
    parser.add_argument("--once", action="store_true", help="process one document and stop")
    arguments = parser.parse_args()
    settings = get_settings()
    validate_runtime_role(settings, "ingestion-worker")
    if not settings.worker_database_url:
        raise SystemExit("CONTRACTOPS_WORKER_DATABASE_URL is required")
    configure_logging(settings.log_level)
    configure_tracing(
        service_name=f"{settings.otel_service_name}-ingestion",
        endpoint=settings.otel_exporter_otlp_endpoint,
    )
    start_metrics_server(settings.ingestion_worker_metrics_port)
    database = WorkerDatabase(settings.worker_database_url)
    store = PostgresIngestionStore(database)
    object_store = MinioObjectStore(
        settings.object_store_endpoint,
        settings.object_store_access_key,
        settings.object_store_secret_key,
        settings.object_store_bucket,
        secure=settings.object_store_secure,
    )
    worker_id = f"{socket.gethostname()}:ingestion"
    suggester = (
        OpenAICompatibleFindingSuggester(settings.model_base_url, settings.document_model_name)
        if settings.document_model_assistance_enabled
        else None
    )
    try:
        while True:
            job = store.claim(worker_id, lease_seconds=settings.ingestion_lease_seconds)
            if job is None:
                if arguments.once:
                    break
                time.sleep(settings.ingestion_poll_interval_seconds)
                continue
            try:
                with maintain_lease(
                    partial(store.renew, job, lease_seconds=settings.ingestion_lease_seconds),
                    interval_seconds=settings.ingestion_lease_seconds / 3,
                ):
                    content = object_store.get_bytes(job.object_key)
                    actual_hash = hashlib.sha256(content).hexdigest()
                    if actual_hash != job.expected_hash:
                        raise ContractOpsError(
                            code="document_hash_mismatch",
                            message="uploaded document hash does not match the declaration",
                            status_code=422,
                        )
                    blocks = parse_document(content, job.media_type)
                    findings = tuple(
                        (block.sequence, finding)
                        for block in blocks
                        for finding in (
                            extract_rule_findings(block)
                            + (() if suggester is None else suggester.suggest(block.content))
                        )
                    )
                    store.succeed(job, blocks, findings)
            except ContractOpsError as error:
                store.fail(job, code=error.code, message=error.message)
            except Exception as error:
                store.fail(job, code="document_ingestion_failed", message=str(error))
            if arguments.once:
                break
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
