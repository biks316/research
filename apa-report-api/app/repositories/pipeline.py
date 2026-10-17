"""Checkpoint storage for generation stages, plus finished report content."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EvidenceCard, Paper, ReportReference, ReportSection, ScreeningResult


class PipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Search checkpoint -----------------------------------------------------
    async def replace_rows_for_sub_question(self, model: type, report_id: uuid.UUID, sub_question_id: str, rows: Iterable[object]) -> None:
        """Partial checkpoint: replace one sub-question's rows of a stage table."""
        await self.session.execute(
            delete(model).where(model.report_id == report_id, model.sub_question_id == sub_question_id)
        )
        self.session.add_all(list(rows))
        await self.session.flush()

    async def papers(self, report_id: uuid.UUID) -> Sequence[Paper]:
        stmt = select(Paper).where(Paper.report_id == report_id).order_by(Paper.sub_question_id, Paper.rank)
        return (await self.session.execute(stmt)).scalars().all()

    # --- Screening checkpoint --------------------------------------------------
    async def screening(self, report_id: uuid.UUID) -> Sequence[ScreeningResult]:
        stmt = select(ScreeningResult).where(ScreeningResult.report_id == report_id).order_by(ScreeningResult.id)
        return (await self.session.execute(stmt)).scalars().all()

    async def screening_counts(self, report_id: uuid.UUID) -> tuple[int, int]:
        """(distinct works screened, distinct works included)."""
        screened = select(func.count(func.distinct(ScreeningResult.work_id))).where(
            ScreeningResult.report_id == report_id
        )
        included = screened.where(ScreeningResult.included.is_(True))
        return (
            int((await self.session.execute(screened)).scalar_one()),
            int((await self.session.execute(included)).scalar_one()),
        )

    # --- Extraction checkpoint -------------------------------------------------
    async def evidence(self, report_id: uuid.UUID) -> Sequence[EvidenceCard]:
        stmt = select(EvidenceCard).where(EvidenceCard.report_id == report_id).order_by(EvidenceCard.id)
        return (await self.session.execute(stmt)).scalars().all()

    # --- Writing / verification checkpoints ------------------------------------
    async def replace_sections(self, report_id: uuid.UUID, sections: Iterable[ReportSection]) -> None:
        await self.session.execute(delete(ReportSection).where(ReportSection.report_id == report_id))
        await self.session.flush()
        self.session.add_all(list(sections))
        await self.session.flush()

    async def sections(self, report_id: uuid.UUID) -> Sequence[ReportSection]:
        stmt = select(ReportSection).where(ReportSection.report_id == report_id).order_by(ReportSection.order)
        return (await self.session.execute(stmt)).scalars().all()

    async def replace_references(self, report_id: uuid.UUID, refs: Iterable[ReportReference]) -> None:
        await self.session.execute(delete(ReportReference).where(ReportReference.report_id == report_id))
        self.session.add_all(list(refs))
        await self.session.flush()

    async def references(self, report_id: uuid.UUID) -> Sequence[ReportReference]:
        stmt = select(ReportReference).where(ReportReference.report_id == report_id).order_by(ReportReference.position)
        return (await self.session.execute(stmt)).scalars().all()
