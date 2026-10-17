"""Screening: score each abstract 0-3 against its specific sub-question."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.clients.llm import LLMClient, embed_input
from app.clients.openalex import Work
from app.config import Settings
from app.models.enums import SubQuestionRole
from app.pipeline.models import ScreeningBatch

ABSTRACT_CHARS = 2000

SYSTEM = """TASK: screen
You screen papers for a systematic literature review. For each paper, judge only whether its
title and abstract help answer the given sub-question (not the topic in general).
Score:
  3 = directly answers the sub-question with empirical evidence or a systematic review
  2 = relevant evidence for part of the sub-question
  1 = only useful as general background or context
  0 = not relevant, or relevant only to a different question
Give a one-line reason (under 25 words) for each score. Score every paper listed, using its
work_id exactly as given. Do not use outside knowledge about the papers. Return only the JSON object."""


@dataclass(frozen=True)
class Screened:
    work_id: str
    score: int
    reason: str
    included: bool


def is_included(score: int, role: SubQuestionRole | str) -> bool:
    """Keep 2-3. A score of 1 is kept only for background sub-questions."""
    return score >= 2 or (score == 1 and SubQuestionRole(role) == SubQuestionRole.BACKGROUND)


async def screen(
    llm: LLMClient,
    settings: Settings,
    *,
    question: str,
    role: SubQuestionRole | str,
    works: Sequence[Work],
) -> list[Screened]:
    results: list[Screened] = []
    size = settings.SCREENING_BATCH_SIZE
    for start in range(0, len(works), size):
        batch = works[start : start + size]
        user = "Screen these papers.\n" + embed_input(
            {
                "subQuestion": question,
                "papers": [
                    {"work_id": w.id, "title": w.title, "year": w.year, "abstract": (w.abstract or "")[:ABSTRACT_CHARS]}
                    for w in batch
                ],
            }
        )
        out = await llm.complete_json(SYSTEM, user, ScreeningBatch, model=settings.LLM_SCREENING_MODEL)
        by_id = {r.work_id.strip().upper(): r for r in out.results}
        for w in batch:
            r = by_id.get(w.id)
            if r is None:  # the model skipped it: treat as not relevant rather than guess
                results.append(Screened(w.id, 0, "Not scored by the screener", False))
            else:
                results.append(Screened(w.id, r.score, r.reason.strip(), is_included(r.score, role)))
    return results
