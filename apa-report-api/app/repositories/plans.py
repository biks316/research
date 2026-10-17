from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Plan, SubQuestion


@dataclass(frozen=True)
class SubQuestionData:
    id: str
    question: str
    rationale: str | None
    role: str


def new_sub_question_id() -> str:
    return f"sq_{uuid.uuid4().hex[:12]}"


class PlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, report_id: uuid.UUID, *, for_update: bool = False) -> Plan | None:
        stmt = select(Plan).where(Plan.report_id == report_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def sub_questions(self, report_id: uuid.UUID) -> Sequence[SubQuestion]:
        stmt = select(SubQuestion).where(SubQuestion.report_id == report_id).order_by(SubQuestion.order)
        return (await self.session.execute(stmt)).scalars().all()

    async def save(
        self,
        report_id: uuid.UUID,
        *,
        central_question: str,
        interpretation_note: str | None,
        sub_questions: Sequence[SubQuestionData],
    ) -> Plan:
        """Create the plan, or overwrite it (used by a retried planning job)."""
        plan = await self.get(report_id, for_update=True)
        if plan is None:
            plan = Plan(report_id=report_id, central_question=central_question, version=1, locked=False)
            self.session.add(plan)
        plan.central_question = central_question
        plan.interpretation_note = interpretation_note
        await self.replace_sub_questions(report_id, sub_questions)
        await self.session.flush()
        return plan

    async def replace_sub_questions(self, report_id: uuid.UUID, items: Sequence[SubQuestionData]) -> None:
        await self.session.execute(delete(SubQuestion).where(SubQuestion.report_id == report_id))
        await self.session.flush()
        for order, item in enumerate(items, start=1):
            self.session.add(
                SubQuestion(
                    id=item.id,
                    report_id=report_id,
                    order=order,
                    question=item.question,
                    rationale=item.rationale,
                    role=item.role,
                )
            )
        await self.session.flush()
