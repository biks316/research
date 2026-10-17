"""Transactional outbox: publish after commit, and a relay that re-publishes unsent rows."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import structlog

from app.config import Settings
from app.core.database import Database
from app.repositories.outbox import OutboxRepository
from app.services.queue import QueuePublisher

log = structlog.get_logger(__name__)


class OutboxDispatcher:
    def __init__(self, db: Database, publisher: QueuePublisher) -> None:
        self._db = db
        self._publisher = publisher

    async def dispatch(self, outbox_ids: Iterable[int]) -> None:
        """Publish the given rows now; failures are left for the relay."""
        for outbox_id in outbox_ids:
            async with self._db.session() as session:
                repo = OutboxRepository(session)
                row = await repo.get(outbox_id, for_update=True)
                if row is None or row.sent_at is not None:
                    await session.rollback()
                    continue
                try:
                    await self._publisher.publish(row.queue, row.payload)
                except Exception as exc:  # noqa: BLE001 - any publish failure is retried by the relay
                    log.warning("outbox_publish_failed", outbox_id=outbox_id, error=type(exc).__name__)
                    await repo.mark_failed(outbox_id, f"{type(exc).__name__}: {exc}")
                else:
                    await repo.mark_sent(outbox_id)
                await session.commit()

    async def relay_once(self, *, batch_size: int, older_than_seconds: float) -> int:
        """Re-publish unsent rows. Returns how many were sent."""
        sent = 0
        async with self._db.session() as session:
            repo = OutboxRepository(session)
            rows = await repo.claim_unsent(limit=batch_size, older_than_seconds=older_than_seconds)
            for row in rows:
                try:
                    await self._publisher.publish(row.queue, row.payload)
                except Exception as exc:  # noqa: BLE001
                    log.warning("outbox_relay_failed", outbox_id=row.id, error=type(exc).__name__)
                    await repo.mark_failed(row.id, f"{type(exc).__name__}: {exc}")
                else:
                    await repo.mark_sent(row.id)
                    sent += 1
            await session.commit()
        if sent:
            log.info("outbox_relayed", count=sent)
        return sent


class OutboxRelay:
    """Background task started in the app lifespan."""

    def __init__(self, dispatcher: OutboxDispatcher, settings: Settings) -> None:
        self._dispatcher = dispatcher
        self._interval = settings.OUTBOX_RELAY_INTERVAL_SECONDS
        self._batch = settings.OUTBOX_RELAY_BATCH_SIZE
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            try:
                # Only rows older than one interval, so we don't race the request path.
                await self._dispatcher.relay_once(batch_size=self._batch, older_than_seconds=self._interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep relaying through transient DB errors
                log.exception("outbox_relay_error")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="outbox-relay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
