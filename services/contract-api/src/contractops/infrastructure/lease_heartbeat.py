from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Thread

logger = logging.getLogger(__name__)


@contextmanager
def maintain_lease(renew: Callable[[], bool], *, interval_seconds: float) -> Iterator[None]:
    """Renew during blocking I/O/parsing; database fencing remains the authority to commit."""
    stopped = Event()

    def heartbeat() -> None:
        while not stopped.wait(interval_seconds):
            try:
                if not renew():
                    logger.warning("ingestion lease lost; stale results will be discarded")
                    return
            except Exception:
                # Fail closed: never extend a lease locally after a database failure.
                logger.exception("ingestion lease renewal failed")
                return

    thread = Thread(target=heartbeat, name="ingestion-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)
