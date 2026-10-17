"""Progress events over Redis pub/sub on channel `report:{id}:progress`."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.schemas.reports import ProgressEvent

log = structlog.get_logger(__name__)


def channel_for(report_id: uuid.UUID | str) -> str:
    return f"report:{report_id}:progress"


class Subscription:
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub

    async def next_event(self, timeout: float) -> ProgressEvent | None:
        """Wait up to `timeout` seconds for the next event."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
            if message is None:
                continue
            if message.get("type") != "message":
                continue
            data = message["data"]
            try:
                return ProgressEvent.model_validate_json(data)
            except ValueError:
                log.warning("progress_event_invalid")


class ProgressBus:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, report_id: uuid.UUID | str, event: ProgressEvent) -> None:
        payload = json.dumps(event.model_dump(mode="json", by_alias=True))
        try:
            await self._redis.publish(channel_for(report_id), payload)
        except Exception as exc:  # noqa: BLE001 - the DB row is the source of truth; pub/sub is best effort
            log.warning("progress_publish_failed", error=type(exc).__name__)

    @asynccontextmanager
    async def subscribe(self, report_id: uuid.UUID | str) -> AsyncIterator[Subscription]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel_for(report_id))
        try:
            yield Subscription(pubsub)
        finally:
            try:
                await pubsub.unsubscribe(channel_for(report_id))
            finally:
                await pubsub.aclose()
