from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Outbox


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, report_id: uuid.UUID, queue: str, payload: dict[str, Any]) -> Outbox:
        row = Outbox(report_id=report_id, queue=queue, payload=payload, attempts=0)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, outbox_id: int, *, for_update: bool = False) -> Outbox | None:
        stmt = select(Outbox).where(Outbox.id == outbox_id)
        if for_update:
            stmt = stmt.with_for_update(skip_locked=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def claim_unsent(self, *, limit: int, older_than_seconds: float) -> Sequence[Outbox]:
        """Lock a batch of unsent rows; other relays skip the locked rows."""
        stmt = (
            select(Outbox)
            .where(
                Outbox.sent_at.is_(None),
                Outbox.created_at < func.now() - timedelta(seconds=older_than_seconds),
            )
            .order_by(Outbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def mark_sent(self, outbox_id: int) -> None:
        await self.session.execute(
            update(Outbox)
            .where(Outbox.id == outbox_id)
            .values(sent_at=func.now(), attempts=Outbox.attempts + 1, last_error=None)
            .execution_options(synchronize_session=False)
        )

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        await self.session.execute(
            update(Outbox)
            .where(Outbox.id == outbox_id)
            .values(attempts=Outbox.attempts + 1, last_error=error[:2000])
            .execution_options(synchronize_session=False)
        )
