from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from redis import Redis
from redis.exceptions import ResponseError

from contractops.domain.events import OutboxEvent, StreamMessage


class RedisEventStream:
    def __init__(
        self,
        redis_url: str,
        *,
        stream_name: str = "contractops.events.v1",
        group_name: str = "contractops.notifications.v1",
    ) -> None:
        self._client: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._stream_name = stream_name
        self._group_name = group_name

    def ensure_group(self) -> None:
        try:
            self._client.xgroup_create(
                self._stream_name,
                self._group_name,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, event: OutboxEvent) -> str:
        return cast(
            str,
            self._client.xadd(
                self._stream_name,
                {
                    "event_id": str(event.id),
                    "tenant_id": str(event.tenant_id),
                    "event_type": event.event_type,
                    "schema_version": str(event.schema_version),
                },
            ),
        )

    def read(self, consumer_name: str, *, count: int, block_ms: int) -> Sequence[StreamMessage]:
        response = self._client.xreadgroup(
            self._group_name,
            consumer_name,
            {self._stream_name: ">"},
            count=count,
            block=block_ms,
        )
        return self._messages(response)

    def claim_stale(
        self, consumer_name: str, *, min_idle_ms: int, count: int
    ) -> Sequence[StreamMessage]:
        response = cast(
            Any,
            self._client.xautoclaim(
                self._stream_name,
                self._group_name,
                consumer_name,
                min_idle_ms,
                start_id="0-0",
                count=count,
            ),
        )
        messages = response[1] if len(response) > 1 else []
        return tuple(self._message(message_id, fields) for message_id, fields in messages)

    def acknowledge(self, message_id: str) -> None:
        self._client.xack(self._stream_name, self._group_name, message_id)

    def close(self) -> None:
        self._client.close()

    @classmethod
    def _messages(cls, response: Any) -> Sequence[StreamMessage]:
        values: list[StreamMessage] = []
        for _, messages in response:
            values.extend(cls._message(message_id, fields) for message_id, fields in messages)
        return tuple(values)

    @staticmethod
    def _message(message_id: str, fields: Mapping[str, str]) -> StreamMessage:
        return StreamMessage(message_id=message_id, event_id=UUID(fields["event_id"]))
