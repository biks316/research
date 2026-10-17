"""Data access for reports. All status changes are conditional updates."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Report
from app.models.enums import NON_TERMINAL_STATUSES, ReportStatus


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, report: Report) -> Report:
        self.session.add(report)
        await self.session.flush()
        return report

    async def get(self, report_id: uuid.UUID, *, for_update: bool = False) -> Report | None:
        stmt = select(Report).where(Report.id == report_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_owned(self, report_id: uuid.UUID, user_id: str, *, for_update: bool = False) -> Report | None:
        stmt = select(Report).where(Report.id == report_id, Report.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(
        self,
        user_id: str,
        *,
        status: ReportStatus | None,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None,
    ) -> Sequence[Report]:
        stmt = select(Report).where(Report.user_id == user_id)
        if status is not None:
            stmt = stmt.where(Report.status == status.value)
        if after is not None:
            stmt = stmt.where(tuple_(Report.created_at, Report.id) < tuple_(*after))
        stmt = stmt.order_by(Report.created_at.desc(), Report.id.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def count_non_terminal(self, user_id: str) -> int:
        stmt = select(func.count()).where(
            Report.user_id == user_id, Report.status.in_([s.value for s in NON_TERMINAL_STATUSES])
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def lock_user(self, user_id: str) -> None:
        """Serialize report creation per user for the rest of the transaction."""
        await self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(user_id))))

    async def conditional_update(
        self,
        report_id: uuid.UUID,
        *,
        expected: Collection[ReportStatus],
        values: dict[str, Any],
        lease_owner: str | None = None,
        user_id: str | None = None,
        lease_must_be_expired: bool = False,
    ) -> Report | None:
        """UPDATE reports SET ... WHERE id = :id AND status IN (:expected) [AND ...] RETURNING *.

        Returns the updated row, or None when no row matched (status moved on, lease lost,
        not owned, or deleted). This is what makes concurrent workers safe.
        """
        stmt = (
            update(Report)
            .where(Report.id == report_id, Report.status.in_([ReportStatus(s).value for s in expected]))
            .values(**values, updated_at=func.now())
            .returning(Report)
            # Refresh any copy of this row already in the session with the RETURNING values.
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        if lease_owner is not None:
            stmt = stmt.where(Report.lease_owner == lease_owner)
        if user_id is not None:
            stmt = stmt.where(Report.user_id == user_id)
        if lease_must_be_expired:
            stmt = stmt.where((Report.lease_expires_at.is_(None)) | (Report.lease_expires_at < func.now()))
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return row

    async def extend_lease(self, report_id: uuid.UUID, owner: str, seconds: int) -> bool:
        stmt = (
            update(Report)
            .where(Report.id == report_id, Report.lease_owner == owner)
            .values(lease_expires_at=func.now() + timedelta(seconds=seconds))
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(stmt)
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def add_token_usage(self, report_id: uuid.UUID, usage: dict[str, int]) -> None:
        report = await self.get(report_id, for_update=True)
        if report is None:
            return
        merged = dict(report.token_usage or {})
        for key, value in usage.items():
            merged[key] = int(merged.get(key, 0)) + int(value)
        report.token_usage = merged

    async def delete(self, report_id: uuid.UUID) -> None:
        await self.session.execute(delete(Report).where(Report.id == report_id))
