"""Generation phase: QUEUED_FOR_GENERATION -> SEARCHING -> ... -> VERIFYING -> COMPLETED.

Each stage saves its output and its checkpoint in one transaction. A retried job resumes after
the last checkpoint, so a worker killed mid-job never repeats finished stages.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from app.clients.openalex import Work, default_filters
from app.models import EvidenceCard, Paper, Report, ReportReference, ReportSection, ScreeningResult
from app.models.enums import GENERATION_STAGES, ErrorCode, JobPhase, ReportStatus
from app.pipeline import apa, extractor, screener
from app.pipeline.retriever import Retriever
from app.pipeline.verifier import Verifier, cited_ids
from app.pipeline.writer import DraftSection, SubQuestionInfo, Writer
from app.repositories.pipeline import PipelineRepository
from app.repositories.plans import PlanRepository
from app.services.report_service import JobCancelled, progress_event
from app.workers.context import JobContext, JobOutcome, PipelineError

log = structlog.get_logger(__name__)
S = ReportStatus

# Progress range covered by each stage.
STAGE_RANGES = {
    S.SEARCHING: (15, 35),
    S.SCREENING: (35, 55),
    S.EXTRACTING: (55, 70),
    S.WRITING: (70, 88),
    S.VERIFYING: (88, 99),
}


def _now() -> datetime:
    return datetime.now(UTC)


class GenerationJob:
    def __init__(self, ctx: JobContext, report: Report) -> None:
        self.ctx = ctx
        self.report = report
        self.report_id = report.id
        self.options: dict[str, Any] = report.options or {}

    # ------------------------------------------------------------ helpers
    async def progress(self, stage: ReportStatus, fraction: float, message: str) -> None:
        low, high = STAGE_RANGES[stage]
        percent = int(low + (high - low) * max(0.0, min(1.0, fraction)))
        await self.ctx.reports.report_progress(self.report_id, self.ctx.worker_id, stage, percent, message)

    async def ensure_not_cancelled(self) -> None:
        status = await self.ctx.reports.status_of(self.report_id)
        if status is None or status == S.CANCELLED:
            raise JobCancelled(str(self.report_id))

    def filters(self) -> dict[str, str]:
        year_to = self.options.get("yearTo") or _now().year
        return default_filters(
            year_from=self.options.get("yearFrom"),
            year_to=year_to if self.options.get("yearFrom") or self.options.get("yearTo") else None,
            open_access_only=bool(self.options.get("openAccessOnly")),
            language=self.options.get("language") or "en",
        )

    def year_range(self) -> str | None:
        year_from = self.options.get("yearFrom")
        if not year_from:
            return None
        return f"{year_from}-{self.options.get('yearTo') or _now().year}"

    async def sub_questions(self) -> list[SubQuestionInfo]:
        async with self.ctx.reports.db.session() as session:
            rows = await PlanRepository(session).sub_questions(self.report_id)
        return [SubQuestionInfo(id=r.id, question=r.question, role=r.role) for r in rows]

    async def central(self) -> tuple[str, str | None]:
        async with self.ctx.reports.db.session() as session:
            plan = await PlanRepository(session).get(self.report_id)
        if plan is None:
            raise PipelineError(ErrorCode.INTERNAL_ERROR, "The research plan is missing.")
        return plan.central_question, plan.interpretation_note

    async def checkpoint(self, stage: ReportStatus, save: Callable[[PipelineRepository], Awaitable[None]], values=None) -> None:
        async with self.ctx.reports.db.session() as session:
            await self.ctx.reports.save_checkpoint(session, self.report_id, self.ctx.worker_id, stage, values)
            await save(PipelineRepository(session))
            await session.commit()
        log.info("checkpoint_saved", stage=stage.value)

    async def save_partial(self, stage: ReportStatus, model: type, sub_question_id: str, rows: list, fraction: float, message: str) -> None:
        """Save one sub-question's output for this stage, so a retry can skip it."""
        low, high = STAGE_RANGES[stage]
        percent = int(low + (high - low) * max(0.0, min(1.0, fraction)))
        async with self.ctx.reports.db.session() as session:
            # Lease-checked update first: it row-locks the report (a concurrent delete waits) and
            # raises JobCancelled if the report was cancelled or deleted.
            report = await self.ctx.reports.save_progress_in(session, self.report_id, self.ctx.worker_id, stage, percent, message)
            await PipelineRepository(session).replace_rows_for_sub_question(model, self.report_id, sub_question_id, rows)
            await session.commit()
        await self.ctx.reports.progress.publish(self.report_id, progress_event(report))

    async def done_sub_questions(self, model: type) -> set[str]:
        async with self.ctx.reports.db.session() as session:
            stmt = select(model.sub_question_id).where(model.report_id == self.report_id).distinct()
            return set((await session.execute(stmt)).scalars().all())

    async def papers_by_key(self) -> dict[tuple[str, str], Paper]:
        async with self.ctx.reports.db.session() as session:
            papers = await PipelineRepository(session).papers(self.report_id)
        return {(p.sub_question_id, p.work_id): p for p in papers}

    # ------------------------------------------------------------- stages
    async def search(self) -> None:
        central, _ = await self.central()
        subs = await self.sub_questions()
        retriever = Retriever(self.ctx.openalex, self.ctx.llm, self.ctx.settings)
        filters = self.filters()
        done = await self.done_sub_questions(Paper)
        for i, sq in enumerate(subs):
            if sq.id in done:
                log.info("search_skipped_already_saved", sub_question_id=sq.id)
                continue
            await self.progress(S.SEARCHING, i / len(subs), f"Searching OpenAlex for sub-question {i + 1} of {len(subs)}")
            candidates = await retriever.retrieve(central_question=central, question=sq.question, filters=filters)
            rows = [
                Paper(
                    report_id=self.report_id,
                    sub_question_id=sq.id,
                    work_id=c.work.id,
                    rank=rank,
                    score=c.score,
                    seed_connections=c.seed_connections,
                    sources=sorted(c.sources),
                    work=c.work.model_dump(mode="json"),
                )
                for rank, c in enumerate(candidates, start=1)
            ]
            await self.save_partial(
                S.SEARCHING, Paper, sq.id, rows, (i + 1) / len(subs), f"Found {len(rows)} candidate papers for sub-question {i + 1} of {len(subs)}"
            )
        if not await self.papers_by_key():
            raise PipelineError(ErrorCode.INSUFFICIENT_SOURCES, "OpenAlex returned no papers matching the plan and filters.")
        methodology = {"database": "OpenAlex", "searchedAt": _now().isoformat().replace("+00:00", "Z"), "papersScreened": 0, "papersIncluded": 0}
        if self.year_range():
            methodology["yearRange"] = self.year_range()

        async def save(repo: PipelineRepository) -> None:
            return None  # rows were saved per sub-question

        await self.checkpoint(S.SEARCHING, save, {"methodology": methodology})

    async def screen(self) -> None:
        subs = await self.sub_questions()
        papers = await self.papers_by_key()
        by_sq: dict[str, list[Work]] = defaultdict(list)
        for (sq_id, _), paper in papers.items():
            by_sq[sq_id].append(Work.model_validate(paper.work))
        total = sum(len(v) for v in by_sq.values())
        already = await self.done_sub_questions(ScreeningResult)
        done = 0
        for sq in subs:
            works = by_sq.get(sq.id, [])
            if sq.id in already or not works:
                done += len(works)
                continue
            await self.progress(S.SCREENING, done / max(1, total), f"Screening {total} candidate papers")
            rows = [
                ScreeningResult(
                    report_id=self.report_id, sub_question_id=sq.id, work_id=r.work_id,
                    score=r.score, reason=r.reason, included=r.included,
                )
                for r in await screener.screen(self.ctx.llm, self.ctx.settings, question=sq.question, role=sq.role, works=works)
            ]
            done += len(works)
            await self.save_partial(S.SCREENING, ScreeningResult, sq.id, rows, done / max(1, total), f"Screened {done} of {total} candidate papers")

        async def save(repo: PipelineRepository) -> None:
            return None  # rows were saved per sub-question

        await self.checkpoint(S.SCREENING, save)

    async def extract(self) -> None:
        subs = await self.sub_questions()
        papers = await self.papers_by_key()
        async with self.ctx.reports.db.session() as session:
            screening = await PipelineRepository(session).screening(self.report_id)
        kept: dict[str, list[tuple[Work, int]]] = defaultdict(list)
        for r in screening:
            if r.included and (r.sub_question_id, r.work_id) in papers:
                kept[r.sub_question_id].append((Work.model_validate(papers[(r.sub_question_id, r.work_id)].work), r.score))
        distinct = {w.id for items in kept.values() for w, _ in items}
        if len(distinct) < self.ctx.settings.MIN_INCLUDED_PAPERS:
            raise PipelineError(
                ErrorCode.INSUFFICIENT_SOURCES,
                f"Only {len(distinct)} relevant papers were found; at least {self.ctx.settings.MIN_INCLUDED_PAPERS} are needed.",
            )
        already = await self.done_sub_questions(EvidenceCard)
        for i, sq in enumerate(subs):
            items = kept.get(sq.id, [])
            if not items or sq.id in already:
                continue
            await self.progress(S.EXTRACTING, i / len(subs), f"Extracting evidence for sub-question {i + 1} of {len(subs)}")
            scores = {w.id: score for w, score in items}
            rows = [
                EvidenceCard(
                    report_id=self.report_id, sub_question_id=sq.id, work_id=card.work_id,
                    finding=card.finding, design=card.design, sample=card.sample,
                    effect_direction=card.effect_direction.value if card.effect_direction else None,
                    claim_strength=card.claim_strength.value if card.claim_strength else None,
                    limitations=card.limitations, background_only=scores.get(card.work_id) == 1,
                )
                for card in await extractor.extract(self.ctx.llm, question=sq.question, works=[w for w, _ in items])
            ]
            await self.save_partial(
                S.EXTRACTING, EvidenceCard, sq.id, rows, (i + 1) / len(subs), f"Extracted evidence for sub-question {i + 1} of {len(subs)}"
            )
        async with self.ctx.reports.db.session() as session:
            if not await PipelineRepository(session).evidence(self.report_id):
                raise PipelineError(ErrorCode.INSUFFICIENT_SOURCES, "No evidence could be extracted from the included papers.")

        async def save(repo: PipelineRepository) -> None:
            return None  # rows were saved per sub-question

        await self.checkpoint(S.EXTRACTING, save)

    async def write(self) -> None:
        central, note = await self.central()
        subs = await self.sub_questions()
        papers = await self.papers_by_key()
        async with self.ctx.reports.db.session() as session:
            cards = await PipelineRepository(session).evidence(self.report_id)
        meta = {p.work_id: {"year": p.work.get("year"), "title": p.work.get("title")} for p in papers.values()}

        async def on_progress(message: str) -> None:
            await self.progress(S.WRITING, 0.5, message)

        sections, front = await Writer(self.ctx.llm, self.ctx.settings).write(
            central_question=central,
            interpretation_note=note,
            sub_questions=subs,
            cards=cards,
            work_meta=meta,
            length=self.options.get("length", "STANDARD"),
            progress=on_progress,
        )
        rows = [
            ReportSection(
                id=f"sec_{uuid.uuid4().hex[:12]}", report_id=self.report_id, order=order, kind=s.kind,
                heading=s.heading, sub_question_id=s.sub_question_id, draft_content=s.content,
            )
            for order, s in enumerate(sections, start=1)
        ]

        async def save(repo: PipelineRepository) -> None:
            await repo.replace_sections(self.report_id, rows)

        await self.checkpoint(
            S.WRITING, save, {"title": front.title, "abstract": front.abstract, "keywords": front.keywords}
        )

    async def verify(self) -> None:
        papers = await self.papers_by_key()
        async with self.ctx.reports.db.session() as session:
            repo = PipelineRepository(session)
            sections = await repo.sections(self.report_id)
            cards = await repo.evidence(self.report_id)
            screened, _ = await repo.screening_counts(self.report_id)
        cards_by_work: dict[str, list[EvidenceCard]] = defaultdict(list)
        for card in cards:
            cards_by_work[card.work_id].append(card)
        known = set(cards_by_work)  # only works that were retrieved, screened in, and extracted
        await self.progress(S.VERIFYING, 0.1, "Verifying citations")
        verified, report = await Verifier(self.ctx.llm).verify(
            [DraftSection(s.kind, s.heading, s.sub_question_id, s.draft_content) for s in sections],
            known_ids=known,
            cards_by_work=cards_by_work,
        )
        cited = list(dict.fromkeys(i for s in verified for i in cited_ids(s.content)))
        if not cited:
            raise PipelineError(ErrorCode.INSUFFICIENT_SOURCES, "No claims in the draft could be supported by the retrieved papers.")
        works_by_id = {p.work_id: Work.model_validate(p.work) for p in papers.values()}
        await self.progress(S.VERIFYING, 0.7, "Formatting APA 7 citations and references")
        refs = apa.format_references([works_by_id[i] for i in cited if i in works_by_id])
        refs_by_id = {r.work_id: r for r in refs}
        updated = []
        for row, v in zip(sections, verified, strict=True):
            row.verified_content = v.content
            row.content = apa.apply_citations(v.content, refs_by_id)
            updated.append(row)
        reference_rows = [
            ReportReference(
                report_id=self.report_id, work_id=r.work_id, position=r.position, apa_citation=r.apa_citation,
                in_text_citation=r.in_text_citation, authors=r.authors, year=r.year, title=r.title, source=r.source,
                volume=r.volume, issue=r.issue, pages=r.pages, doi=r.doi, url=r.url, is_open_access=r.is_open_access,
            )
            for r in refs
        ]
        async with self.ctx.reports.db.session() as session:
            current = (await session.get(Report, self.report_id))
            methodology = dict((current.methodology if current else None) or {})
        methodology.update({"papersScreened": screened, "papersIncluded": len(known)})
        log.info("verification_summary", removed=report.removed_citations, rewritten=report.rewritten, dropped=report.dropped)

        async def save(repo: PipelineRepository) -> None:
            await repo.replace_sections(self.report_id, [_copy_section(s) for s in updated])
            await repo.replace_references(self.report_id, reference_rows)

        await self.checkpoint(S.VERIFYING, save, {"methodology": methodology})

    # ---------------------------------------------------------------- run
    async def run(self) -> JobOutcome:
        stages = {
            S.SEARCHING: self.search,
            S.SCREENING: self.screen,
            S.EXTRACTING: self.extract,
            S.WRITING: self.write,
            S.VERIFYING: self.verify,
        }
        checkpoint = self.report.checkpoint_stage
        start = GENERATION_STAGES.index(S(checkpoint)) + 1 if checkpoint else 0
        if start:
            log.info("resuming_after_checkpoint", checkpoint=checkpoint)
        for stage in GENERATION_STAGES[start:]:
            await self.ensure_not_cancelled()
            await self.ctx.reports.advance(self.report_id, self.ctx.worker_id, stage)
            await stages[stage]()
        await self.ensure_not_cancelled()
        await self.ctx.reports.advance(
            self.report_id,
            self.ctx.worker_id,
            S.COMPLETED,
            values={"completed_at": _now(), "lease_owner": None, "lease_expires_at": None},
        )
        log.info("generation_done")
        return JobOutcome.COMPLETED


def _copy_section(s: ReportSection) -> ReportSection:
    return ReportSection(
        id=s.id, report_id=s.report_id, order=s.order, kind=s.kind, heading=s.heading,
        sub_question_id=s.sub_question_id, draft_content=s.draft_content,
        verified_content=s.verified_content, content=s.content,
    )


async def run(ctx: JobContext, report_id: uuid.UUID) -> JobOutcome:
    report = await ctx.reports.claim(report_id, JobPhase.GENERATE, ctx.worker_id)
    if report is None:
        log.info("generation_claim_failed")
        return JobOutcome.SKIPPED
    return await GenerationJob(ctx, report).run()
