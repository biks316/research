"""Business rules and status transitions for reports.

Every status change goes through `_transition`, which checks the transition table and
applies it as a conditional UPDATE (`... WHERE id = :id AND status IN (:expected)`), so two
workers - or a worker and a user cancelling - can never both win.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.database import Database
from app.core.errors import (
    InvalidReportStatus,
    PlanVersionConflict,
    ReportNotFound,
    ReportNotReady,
    TooManyRequests,
    ValidationProblem,
)
from app.models import Report
from app.models.enums import (
    ACTIVE_STATUSES,
    NON_TERMINAL_STATUSES,
    JobPhase,
    QueueName,
    ReportStatus,
)
from app.repositories.outbox import OutboxRepository
from app.repositories.pipeline import PipelineRepository
from app.repositories.plans import PlanRepository, SubQuestionData, new_sub_question_id
from app.repositories.reports import ReportRepository
from app.schemas import (
    ErrorInfo,
    Plan,
    PlanApprovalRequest,
    PlanUpdateRequest,
    ProgressEvent,
    ReferenceList,
    ReportAccepted,
    ReportCreateRequest,
    ReportPage,
    ReportStatusResponse,
    ReportSummary,
    SubQuestion,
)
from app.schemas.output import Author, Methodology, Reference, ReportSection
from app.schemas.output import Report as ReportOut
from app.schemas.problem import FieldError
from app.services.outbox import OutboxDispatcher
from app.services.progress import ProgressBus

log = structlog.get_logger(__name__)

S = ReportStatus
# Forward transitions; FAILED and CANCELLED are handled by the rules below.
FORWARD_TRANSITIONS: dict[ReportStatus, frozenset[ReportStatus]] = {
    S.QUEUED: frozenset({S.PLANNING}),
    S.PLANNING: frozenset({S.AWAITING_APPROVAL}),
    S.AWAITING_APPROVAL: frozenset({S.QUEUED_FOR_GENERATION}),
    S.QUEUED_FOR_GENERATION: frozenset({S.SEARCHING}),
    S.SEARCHING: frozenset({S.SCREENING}),
    S.SCREENING: frozenset({S.EXTRACTING}),
    S.EXTRACTING: frozenset({S.WRITING}),
    S.WRITING: frozenset({S.VERIFYING}),
    S.VERIFYING: frozenset({S.COMPLETED}),
}

# Baseline progress when a status is entered.
STATUS_PROGRESS: dict[ReportStatus, int] = {
    S.QUEUED: 0,
    S.PLANNING: 5,
    S.AWAITING_APPROVAL: 10,
    S.QUEUED_FOR_GENERATION: 10,
    S.SEARCHING: 15,
    S.SCREENING: 35,
    S.EXTRACTING: 55,
    S.WRITING: 70,
    S.VERIFYING: 88,
    S.COMPLETED: 100,
}


def is_allowed(current: ReportStatus, target: ReportStatus) -> bool:
    if target == S.FAILED:
        return current in ACTIVE_STATUSES
    if target == S.CANCELLED:
        return current in NON_TERMINAL_STATUSES
    return target in FORWARD_TRANSITIONS.get(current, frozenset())


def sources_for(target: ReportStatus) -> frozenset[ReportStatus]:
    return frozenset(s for s in ReportStatus if is_allowed(s, target))


class JobCancelled(Exception):
    """The report was cancelled, deleted, or the worker lost its lease."""


@dataclass(frozen=True)
class CreateResult:
    accepted: ReportAccepted
    location: str


def _now() -> datetime:
    return datetime.now(UTC)


def encode_cursor(created_at: datetime, report_id: uuid.UUID) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": str(report_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        created_at = datetime.fromisoformat(data["c"])
        if created_at.tzinfo is None:
            raise ValueError("naive timestamp")
        return created_at, uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError):
        raise ValidationProblem(
            "cursor is invalid", errors=[FieldError(field="cursor", message="is not a valid cursor")]
        ) from None


class ReportService:
    def __init__(
        self,
        db: Database,
        dispatcher: OutboxDispatcher,
        progress: ProgressBus,
        settings: Settings,
    ) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self.progress = progress
        self.settings = settings

    # ------------------------------------------------------------------ helpers
    def status_url(self, report_id: uuid.UUID) -> str:
        return f"{self.settings.API_PREFIX}/reports/{report_id}/status"

    def report_url(self, report_id: uuid.UUID) -> str:
        return f"{self.settings.API_PREFIX}/reports/{report_id}"

    @staticmethod
    async def _owned(session: AsyncSession, report_id: uuid.UUID, user_id: str, *, for_update: bool = False) -> Report:
        report = await ReportRepository(session).get_owned(report_id, user_id, for_update=for_update)
        if report is None:
            raise ReportNotFound()
        return report

    async def _transition(
        self,
        session: AsyncSession,
        report_id: uuid.UUID,
        target: ReportStatus,
        *,
        expected: Collection[ReportStatus] | None = None,
        lease_owner: str | None = None,
        user_id: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> Report | None:
        allowed = sources_for(target)
        expected_set = allowed if expected is None else frozenset(expected) & allowed
        if not expected_set:
            raise ValueError(f"No allowed transition into {target}")
        updates: dict[str, Any] = {"status": target.value}
        if target in STATUS_PROGRESS:
            updates["progress_percent"] = STATUS_PROGRESS[target]
        updates.update(values or {})
        return await ReportRepository(session).conditional_update(
            report_id, expected=expected_set, values=updates, lease_owner=lease_owner, user_id=user_id
        )

    async def _publish(self, report: Report) -> None:
        await self.progress.publish(report.id, progress_event(report))

    # --------------------------------------------------------------- API: create
    async def create_report(self, user_id: str, body: ReportCreateRequest) -> CreateResult:
        options = (body.options.model_dump(mode="json", by_alias=True) if body.options else {}) or {}
        options = {k: v for k, v in options.items() if v is not None}
        async with self.db.session() as session:
            reports = ReportRepository(session)
            await reports.lock_user(user_id)
            active = await reports.count_non_terminal(user_id)
            if active >= self.settings.MAX_ACTIVE_REPORTS_PER_USER:
                raise TooManyRequests(
                    f"You already have {active} reports in progress "
                    f"(limit {self.settings.MAX_ACTIVE_REPORTS_PER_USER}). Wait for one to finish or delete one.",
                    retry_after=self.settings.ACTIVE_LIMIT_RETRY_AFTER_SECONDS,
                    code="ACTIVE_REPORT_LIMIT",
                )
            report = await reports.add(
                Report(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    topic=body.topic,
                    description=body.description,
                    options=options,
                    status=S.QUEUED.value,
                    progress_percent=0,
                    message="Queued for planning",
                    token_usage={},
                    content_version=1,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            outbox = await OutboxRepository(session).add(
                report.id, QueueName.PLANNING.value, {"reportId": str(report.id), "phase": JobPhase.PLAN.value}
            )
            await session.commit()
        await self.dispatcher.dispatch([outbox.id])
        log.info("report_created", report_id=str(report.id))
        accepted = ReportAccepted(
            id=report.id, status=S.QUEUED, status_url=self.status_url(report.id), created_at=report.created_at
        )
        return CreateResult(accepted=accepted, location=self.report_url(report.id))

    # ----------------------------------------------------------------- API: read
    async def list_reports(
        self, user_id: str, *, status: ReportStatus | None, limit: int, cursor: str | None
    ) -> ReportPage:
        after = decode_cursor(cursor) if cursor else None
        async with self.db.session() as session:
            rows = await ReportRepository(session).list_for_user(
                user_id, status=status, limit=limit + 1, after=after
            )
        items = [ReportSummary.model_validate(r) for r in rows[:limit]]
        next_cursor = encode_cursor(rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
        return ReportPage(items=items, next_cursor=next_cursor)

    async def get_status(self, user_id: str, report_id: uuid.UUID) -> ReportStatusResponse:
        async with self.db.session() as session:
            report = await self._owned(session, report_id, user_id)
        return status_response(report)

    async def ensure_owned(self, user_id: str, report_id: uuid.UUID) -> None:
        async with self.db.session() as session:
            await self._owned(session, report_id, user_id)

    async def current_event(self, report_id: uuid.UUID) -> ProgressEvent | None:
        async with self.db.session() as session:
            report = await ReportRepository(session).get(report_id)
        return progress_event(report) if report else None

    async def get_report(self, user_id: str, report_id: uuid.UUID) -> ReportOut:
        async with self.db.session() as session:
            report = await self._owned(session, report_id, user_id)
            self._require_completed(report)
            plan = await PlanRepository(session).get(report_id)
            pipeline = PipelineRepository(session)
            sections = await pipeline.sections(report_id)
            references = await pipeline.references(report_id)
        return ReportOut(
            id=report.id,
            status=S(report.status),
            title=report.title or report.topic,
            topic=report.topic,
            central_question=plan.central_question if plan else report.topic,
            abstract=report.abstract,
            keywords=report.keywords,
            sections=[
                ReportSection(
                    id=s.id,
                    order=s.order,
                    heading=s.heading,
                    sub_question_id=s.sub_question_id,
                    content=s.content or "",
                )
                for s in sections
            ],
            references=[reference_out(r) for r in references],
            methodology=Methodology.model_validate(report.methodology or {}),
            created_at=report.created_at,
            completed_at=report.completed_at or report.updated_at,
        )

    async def get_references(self, user_id: str, report_id: uuid.UUID) -> ReferenceList:
        async with self.db.session() as session:
            report = await self._owned(session, report_id, user_id)
            self._require_completed(report)
            references = await PipelineRepository(session).references(report_id)
        return ReferenceList(items=[reference_out(r) for r in references])

    @staticmethod
    def _require_completed(report: Report) -> None:
        if report.status != S.COMPLETED:
            raise ReportNotReady(
                f"Report is in status {report.status}; this resource is available when COMPLETED."
            )

    # ----------------------------------------------------------------- API: plan
    async def get_plan(self, user_id: str, report_id: uuid.UUID) -> Plan:
        async with self.db.session() as session:
            report = await self._owned(session, report_id, user_id)
            plans = PlanRepository(session)
            plan = await plans.get(report_id)
            if plan is None or report.status in (S.QUEUED, S.PLANNING):
                raise ReportNotReady(
                    f"Report is in status {report.status}; the plan is available from AWAITING_APPROVAL."
                )
            sub_questions = await plans.sub_questions(report_id)
        return plan_out(plan, sub_questions, editable=report.status == S.AWAITING_APPROVAL and not plan.locked)

    async def update_plan(self, user_id: str, report_id: uuid.UUID, body: PlanUpdateRequest) -> Plan:
        async with self.db.session() as session:
            report = await self._owned(session, report_id, user_id, for_update=True)
            plans = PlanRepository(session)
            plan = await plans.get(report_id, for_update=True)
            if report.status != S.AWAITING_APPROVAL or plan is None or plan.locked:
                raise InvalidReportStatus(
                    f"Report is in status {report.status}; the plan can only be edited while AWAITING_APPROVAL."
                )
            if body.version != plan.version:
                raise PlanVersionConflict(
                    f"Plan was modified (current version {plan.version}, request version {body.version}). "
                    "Reload and retry."
                )
            existing_ids = {sq.id for sq in await plans.sub_questions(report_id)}
            errors: list[FieldError] = []
            seen: set[str] = set()
            for index, item in enumerate(body.sub_questions):
                if item.id is None:
                    continue
                if item.id not in existing_ids:
                    errors.append(FieldError(field=f"subQuestions[{index}].id", message="does not match an existing sub-question"))
                elif item.id in seen:
                    errors.append(FieldError(field=f"subQuestions[{index}].id", message="is used more than once"))
                seen.add(item.id)
            if errors:
                raise ValidationProblem(f"{errors[0].field} {errors[0].message}", errors=errors)
            await plans.replace_sub_questions(
                report_id,
                [
                    SubQuestionData(
                        id=item.id or new_sub_question_id(),
                        question=item.question,
                        rationale=item.rationale,
                        role=item.role.value,
                    )
                    for item in body.sub_questions
                ],
            )
            plan.central_question = body.central_question
            plan.version += 1
            plan.updated_at = _now()
            await session.flush()
            sub_questions = await plans.sub_questions(report_id)
            result = plan_out(plan, sub_questions, editable=True)
            await session.commit()
        return result

    async def approve_plan(self, user_id: str, report_id: uuid.UUID, body: PlanApprovalRequest) -> ReportAccepted:
        async with self.db.session() as session:
            report = await self._owned(session, report_id, user_id, for_update=True)
            plans = PlanRepository(session)
            plan = await plans.get(report_id, for_update=True)
            if report.status != S.AWAITING_APPROVAL or plan is None or plan.locked:
                raise InvalidReportStatus(
                    f"Report is in status {report.status}; the plan can only be approved while AWAITING_APPROVAL."
                )
            if body.version != plan.version:
                raise PlanVersionConflict(
                    f"Plan was modified (current version {plan.version}, request version {body.version}). "
                    "Reload and retry."
                )
            updated = await self._transition(
                session,
                report_id,
                S.QUEUED_FOR_GENERATION,
                expected={S.AWAITING_APPROVAL},
                user_id=user_id,
                values={"message": "Plan approved; queued for generation", "checkpoint_stage": None},
            )
            if updated is None:  # pragma: no cover - guarded by the row lock above
                raise InvalidReportStatus("Report status changed; reload and retry.")
            plan.locked = True
            outbox = await OutboxRepository(session).add(
                report_id, QueueName.GENERATION.value, {"reportId": str(report_id), "phase": JobPhase.GENERATE.value}
            )
            await session.commit()
        await self.dispatcher.dispatch([outbox.id])
        await self._publish(updated)
        return ReportAccepted(
            id=report_id,
            status=S.QUEUED_FOR_GENERATION,
            status_url=self.status_url(report_id),
            created_at=updated.created_at,
        )

    # --------------------------------------------------------------- API: delete
    async def cancel_for_delete(self, user_id: str, report_id: uuid.UUID) -> Report:
        """Cancel the report if it is still processing. Returns the report row."""
        async with self.db.session() as session:
            report = await self._owned(session, report_id, user_id)
            cancelled = None
            if S(report.status) in NON_TERMINAL_STATUSES:
                cancelled = await self._transition(
                    session,
                    report_id,
                    S.CANCELLED,
                    user_id=user_id,
                    values={"message": "Cancelled by user", "lease_owner": None, "lease_expires_at": None},
                )
            await session.commit()
        if cancelled is not None:
            await self._publish(cancelled)
            return cancelled
        return report

    async def delete_report_rows(self, report_id: uuid.UUID) -> None:
        async with self.db.session() as session:
            await ReportRepository(session).delete(report_id)
            await session.commit()

    # ------------------------------------------------------------- worker side
    async def claim(self, report_id: uuid.UUID, phase: JobPhase, owner: str) -> Report | None:
        """Claim a job. Returns the report, or None if another worker has it or it is finished.

        A fresh job moves QUEUED -> PLANNING or QUEUED_FOR_GENERATION -> SEARCHING. A job whose
        previous worker died (its lease expired) is resumed in its current status.
        """
        lease = {
            "lease_owner": owner,
            "lease_expires_at": func.now() + timedelta(seconds=self.settings.lease_seconds),
        }
        if phase == JobPhase.PLAN:
            fresh, running = S.PLANNING, {S.PLANNING}
        else:
            fresh, running = S.SEARCHING, {S.SEARCHING, S.SCREENING, S.EXTRACTING, S.WRITING, S.VERIFYING}
        async with self.db.session() as session:
            report = await self._transition(
                session, report_id, fresh, values={**lease, "message": _entry_message(fresh)}
            )
            if report is None:
                report = await ReportRepository(session).conditional_update(
                    report_id, expected=running, values=lease, lease_must_be_expired=True
                )
                if report is not None:
                    log.info("job_resumed", report_id=str(report_id), status=report.status)
            await session.commit()
        if report is not None:
            await self._publish(report)
        return report

    async def extend_lease(self, report_id: uuid.UUID, owner: str) -> bool:
        async with self.db.session() as session:
            ok = await ReportRepository(session).extend_lease(
                report_id, owner, self.settings.lease_seconds
            )
            await session.commit()
        return ok

    async def advance(
        self,
        report_id: uuid.UUID,
        owner: str,
        target: ReportStatus,
        *,
        message: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> Report:
        """Move to `target` (or stay, if already there after a resume). Raises JobCancelled."""
        async with self.db.session() as session:
            report = await self._transition(
                session,
                report_id,
                target,
                lease_owner=owner,
                values={"message": message or _entry_message(target), **(values or {})},
            )
            if report is None:
                # Already in the target status (resumed job)? Then just refresh the message.
                report = await ReportRepository(session).conditional_update(
                    report_id,
                    expected={target},
                    lease_owner=owner,
                    values={"message": message or _entry_message(target), **(values or {})},
                )
            await session.commit()
        if report is None:
            raise JobCancelled(str(report_id))
        await self._publish(report)
        return report

    async def report_progress(
        self, report_id: uuid.UUID, owner: str, status: ReportStatus, percent: int, message: str
    ) -> None:
        async with self.db.session() as session:
            report = await ReportRepository(session).conditional_update(
                report_id,
                expected={status},
                lease_owner=owner,
                values={"progress_percent": max(0, min(100, percent)), "message": message},
            )
            await session.commit()
        if report is None:
            raise JobCancelled(str(report_id))
        await self._publish(report)

    async def save_progress_in(
        self,
        session: AsyncSession,
        report_id: uuid.UUID,
        owner: str,
        status: ReportStatus,
        percent: int,
        message: str,
    ) -> Report:
        """Progress update inside the caller's transaction (with partial stage data)."""
        report = await ReportRepository(session).conditional_update(
            report_id,
            expected={status},
            lease_owner=owner,
            values={"progress_percent": max(0, min(100, percent)), "message": message},
        )
        if report is None:
            raise JobCancelled(str(report_id))
        return report

    async def save_checkpoint(
        self,
        session: AsyncSession,
        report_id: uuid.UUID,
        owner: str,
        stage: ReportStatus,
        values: dict[str, Any] | None = None,
    ) -> None:
        """Record a finished stage inside the caller's transaction (together with the stage's data)."""
        report = await ReportRepository(session).conditional_update(
            report_id,
            expected={stage},
            lease_owner=owner,
            values={"checkpoint_stage": stage.value, **(values or {})},
        )
        if report is None:
            raise JobCancelled(str(report_id))

    async def finish_planning(
        self,
        report_id: uuid.UUID,
        owner: str,
        *,
        central_question: str,
        interpretation_note: str | None,
        sub_questions: list[SubQuestionData],
    ) -> Report:
        """Save the plan and move PLANNING -> AWAITING_APPROVAL in one transaction."""
        async with self.db.session() as session:
            await PlanRepository(session).save(
                report_id,
                central_question=central_question,
                interpretation_note=interpretation_note,
                sub_questions=sub_questions,
            )
            report = await self._transition(
                session,
                report_id,
                S.AWAITING_APPROVAL,
                lease_owner=owner,
                values={"message": _entry_message(S.AWAITING_APPROVAL), "lease_owner": None, "lease_expires_at": None},
            )
            if report is None:
                await session.rollback()
                raise JobCancelled(str(report_id))
            await session.commit()
        await self._publish(report)
        return report

    async def fail(self, report_id: uuid.UUID, code: str, message: str, *, owner: str | None = None) -> bool:
        async with self.db.session() as session:
            report = await self._transition(
                session,
                report_id,
                S.FAILED,
                lease_owner=owner,
                values={
                    "error_code": code,
                    "error_message": message[:2000],
                    "message": "Report generation failed",
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
            await session.commit()
        if report is not None:
            log.warning("report_failed", report_id=str(report_id), code=code)
            await self._publish(report)
        return report is not None

    async def release_lease(self, report_id: uuid.UUID, owner: str) -> None:
        async with self.db.session() as session:
            await ReportRepository(session).conditional_update(
                report_id,
                expected=ACTIVE_STATUSES | {S.AWAITING_APPROVAL, S.COMPLETED},
                lease_owner=owner,
                values={"lease_owner": None, "lease_expires_at": None},
            )
            await session.commit()

    async def status_of(self, report_id: uuid.UUID) -> ReportStatus | None:
        async with self.db.session() as session:
            report = await ReportRepository(session).get(report_id)
        return S(report.status) if report else None

    async def add_token_usage(self, report_id: uuid.UUID, usage: dict[str, int]) -> None:
        if not usage:
            return
        async with self.db.session() as session:
            await ReportRepository(session).add_token_usage(report_id, usage)
            await session.commit()


_ENTRY_MESSAGES = {
    S.PLANNING: "Planning the research questions",
    S.AWAITING_APPROVAL: "Research plan ready for review",
    S.QUEUED_FOR_GENERATION: "Queued for generation",
    S.SEARCHING: "Searching OpenAlex",
    S.SCREENING: "Screening candidate papers",
    S.EXTRACTING: "Extracting evidence from included papers",
    S.WRITING: "Writing the report",
    S.VERIFYING: "Verifying citations",
    S.COMPLETED: "Report completed",
}


def _entry_message(status: ReportStatus) -> str:
    return _ENTRY_MESSAGES.get(status, status.value.replace("_", " ").capitalize())


# ------------------------------------------------------------------- mappers
def status_response(report: Report) -> ReportStatusResponse:
    error = None
    if report.status == S.FAILED:
        error = ErrorInfo(code=report.error_code or "INTERNAL_ERROR", message=report.error_message or "Report generation failed")
    return ReportStatusResponse(
        id=report.id,
        status=S(report.status),
        progress_percent=report.progress_percent,
        message=report.message,
        updated_at=report.updated_at,
        error=error,
    )


def progress_event(report: Report) -> ProgressEvent:
    return ProgressEvent(
        status=S(report.status),
        progress_percent=report.progress_percent,
        message=report.message,
        timestamp=report.updated_at or _now(),
    )


def plan_out(plan: Any, sub_questions: Any, *, editable: bool) -> Plan:
    return Plan(
        central_question=plan.central_question,
        interpretation_note=plan.interpretation_note,
        sub_questions=[
            SubQuestion(id=sq.id, order=sq.order, question=sq.question, rationale=sq.rationale, role=sq.role)
            for sq in sub_questions
        ],
        version=plan.version,
        editable=editable,
    )


def reference_out(ref: Any) -> Reference:
    return Reference(
        id=ref.work_id,
        apa_citation=ref.apa_citation,
        in_text_citation=ref.in_text_citation,
        authors=[Author(**a) for a in ref.authors],
        year=ref.year,
        title=ref.title,
        source=ref.source,
        volume=ref.volume,
        issue=ref.issue,
        pages=ref.pages,
        doi=ref.doi,
        url=ref.url,
        is_open_access=ref.is_open_access,
    )
