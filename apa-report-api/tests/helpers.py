"""Helpers that put reports into specific states directly through the database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from app.models import Plan, Report, ReportReference, ReportSection, SubQuestion


async def set_status(app, report_id: str, status: str, **values: Any) -> None:
    async with app.state.resources.db.session() as session:
        await session.execute(update(Report).where(Report.id == uuid.UUID(report_id)).values(status=status, **values))
        await session.commit()


async def seed_plan(app, report_id: str, *, version: int = 1, locked: bool = False, n: int = 3) -> list[str]:
    rid = uuid.UUID(report_id)
    ids = [f"sq_seed{i}" for i in range(1, n + 1)]
    roles = ["BACKGROUND"] + ["CORE"] * (n - 2) + ["DISCUSSION"] if n >= 3 else ["CORE"] * n
    async with app.state.resources.db.session() as session:
        session.add(
            Plan(
                report_id=rid,
                central_question="Does social media use cause anxiety in adolescents?",
                interpretation_note="Interpreted 'teen' as ages 13-19.",
                version=version,
                locked=locked,
            )
        )
        for i, (sq_id, role) in enumerate(zip(ids, roles, strict=True), start=1):
            session.add(
                SubQuestion(
                    id=sq_id,
                    report_id=rid,
                    order=i,
                    question=f"Seed sub-question number {i}?",
                    rationale=f"Rationale {i}" if i % 2 else None,
                    role=role,
                )
            )
        await session.commit()
    return ids


async def awaiting_approval(app, report_id: str, **kwargs: Any) -> list[str]:
    ids = await seed_plan(app, report_id, **kwargs)
    await set_status(app, report_id, "AWAITING_APPROVAL", progress_percent=10)
    return ids


async def complete(app, report_id: str) -> None:
    """Mark a report COMPLETED with two sections and two references."""
    rid = uuid.UUID(report_id)
    await seed_plan(app, report_id, locked=True)
    async with app.state.resources.db.session() as session:
        session.add_all(
            [
                ReportSection(
                    id="sec_1", report_id=rid, order=1, kind="introduction", heading="Introduction",
                    sub_question_id=None, draft_content="x [W1]", content="Teens use social media (Smith & Lee, 2021).",
                ),
                ReportSection(
                    id="sec_2", report_id=rid, order=2, kind="body", heading="Usage and Anxiety",
                    sub_question_id="sq_seed2", draft_content="y [W2]",
                    content="Heavy use is associated with anxiety (Zhang et al., 2020).",
                ),
                ReportReference(
                    report_id=rid, work_id="W1", position=1,
                    apa_citation="Smith, J., & Lee, K. (2021). Teens online. *Journal of Youth*, *4*(2), 1-10. https://doi.org/10.1/abc",
                    in_text_citation="(Smith & Lee, 2021)",
                    authors=[{"family": "Smith", "given": "J."}, {"family": "Lee", "given": "K."}],
                    year=2021, title="Teens online", source="Journal of Youth", volume="4", issue="2", pages="1-10",
                    doi="https://doi.org/10.1/abc", url="https://doi.org/10.1/abc", is_open_access=True,
                ),
                ReportReference(
                    report_id=rid, work_id="W2", position=2,
                    apa_citation="Zhang, L., Wu, P., & Chen, H. (2020). Anxiety and screens. *Psych Reports*.",
                    in_text_citation="(Zhang et al., 2020)",
                    authors=[{"family": "Zhang", "given": "L."}, {"family": "Wu", "given": "P."}, {"family": "Chen", "given": None}],
                    year=2020, title="Anxiety and screens", source="Psych Reports", volume=None, issue=None, pages=None,
                    doi=None, url="https://openalex.org/W2", is_open_access=False,
                ),
            ]
        )
        await session.commit()
    await set_status(
        app,
        report_id,
        "COMPLETED",
        progress_percent=100,
        title="Social Media Use and Adolescent Anxiety: A Review",
        abstract="This review examines the evidence.",
        keywords=["social media", "anxiety"],
        methodology={
            "database": "OpenAlex",
            "searchedAt": "2026-10-17T19:10:00Z",
            "yearRange": "2010-2026",
            "papersScreened": 40,
            "papersIncluded": 2,
        },
        completed_at=datetime.now(UTC),
    )
