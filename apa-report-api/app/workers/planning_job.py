"""Planning phase: QUEUED -> PLANNING -> AWAITING_APPROVAL."""

from __future__ import annotations

import uuid

import structlog

from app.models.enums import JobPhase
from app.pipeline import planner
from app.repositories.plans import SubQuestionData, new_sub_question_id
from app.workers.context import JobContext, JobOutcome

log = structlog.get_logger(__name__)


async def run(ctx: JobContext, report_id: uuid.UUID) -> JobOutcome:
    report = await ctx.reports.claim(report_id, JobPhase.PLAN, ctx.worker_id)
    if report is None:
        log.info("planning_claim_failed")
        return JobOutcome.SKIPPED
    await ctx.reports.report_progress(report_id, ctx.worker_id, report.status, 6, "Analyzing the topic")
    draft = await planner.plan(ctx.llm, topic=report.topic, description=report.description, options=report.options or {})
    await ctx.reports.finish_planning(
        report_id,
        ctx.worker_id,
        central_question=draft.central_question,
        interpretation_note=draft.interpretation_note,
        sub_questions=[
            SubQuestionData(id=new_sub_question_id(), question=sq.question, rationale=sq.rationale, role=sq.role.value)
            for sq in draft.sub_questions
        ],
    )
    log.info("planning_done", sub_questions=len(draft.sub_questions), assumptions=len(draft.hidden_assumptions))
    return JobOutcome.COMPLETED
