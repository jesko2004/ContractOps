from threading import Event
from threading import enumerate as enumerate_threads

from contractops.infrastructure.lease_heartbeat import maintain_lease


def test_heartbeat_renews_while_work_is_blocked_and_stops_after_exit() -> None:
    renewed = Event()
    calls: list[int] = []

    def renew() -> bool:
        calls.append(1)
        renewed.set()
        return True

    with maintain_lease(renew, interval_seconds=0.01):
        assert renewed.wait(2)
    assert len(calls) >= 1
    assert not any(thread.name == "ingestion-lease" for thread in enumerate_threads())


def test_heartbeat_stops_after_losing_ownership() -> None:
    renewed = Event()
    calls: list[int] = []

    def renew() -> bool:
        calls.append(1)
        renewed.set()
        return False

    with maintain_lease(renew, interval_seconds=0.01):
        assert renewed.wait(2)
    assert calls == [1]
