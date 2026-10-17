"""Writing: section by section, from evidence cards only, with [W123] citation placeholders."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.clients.llm import LLMClient, embed_input
from app.config import Settings
from app.models.enums import SubQuestionRole
from app.pipeline.models import FrontMatter, SectionDraft

WORD_TARGETS = {"SHORT": 1500, "STANDARD": 3000, "LONG": 5000}
SHARES = {"introduction": 0.12, "body": 0.55, "discussion": 0.15, "limitations": 0.09, "conclusion": 0.06}
PLACEHOLDER = re.compile(r"\[(W\d+(?:\s*[;,]\s*W\d+)*)\]")

SECTION_SYSTEM = """TASK: write_section
You write one section of an APA 7 literature review, in Markdown, from the evidence cards given.

Rules:
- Use ONLY the evidence cards provided. Do not add facts, statistics, studies or authors that are
  not on a card, and do not use outside knowledge.
- Cite every empirical claim with the card's placeholder, e.g. [W123], or [W123; W456] for several,
  placed at the end of the sentence before the period. Cite only work_ids that appear on the cards.
  Never write author names, years, or reference entries yourself.
- Match wording to each card's claim_strength:
  causal -> "led to", "reduced", "caused" are allowed;
  predictive -> "predicted", "was followed by";
  associational -> "was associated with", "was linked to" - never causal verbs;
  descriptive or null -> "reported", "described", "found that".
  When cards disagree, say so and explain possible reasons (design, sample, measures).
- Organize the section by theme across studies, not study by study. You may use "### " sub-headings
  inside body sections. Write paragraphs of connected prose; no bullet lists.
- Formal academic tone, third person, past tense for findings. Aim for the word target.
- Do not include the section heading in content; put a concise heading in `heading`.
Return only the JSON object."""

FRONT_SYSTEM = """TASK: front_matter
You write the front matter of an APA 7 literature review from its drafted sections.
- title: a concise, informative title (no citations; under 20 words).
- abstract: 150-250 words, one paragraph, summarizing purpose, method (a review of studies from
  OpenAlex), main findings and implications. No citations and no [W…] placeholders.
- keywords: 3-6 keywords in lowercase.
Base everything only on the sections given. Return only the JSON object."""


@dataclass
class DraftSection:
    kind: str
    heading: str
    sub_question_id: str | None
    content: str


@dataclass(frozen=True)
class SubQuestionInfo:
    id: str
    question: str
    role: str


ProgressFn = Callable[[str], Awaitable[None]]


def _card_payload(card: Any, meta: dict[str, dict[str, Any]]) -> dict[str, Any]:
    info = meta.get(card.work_id, {})
    return {
        "work_id": card.work_id,
        "cite_as": f"[{card.work_id}]",
        "year": info.get("year"),
        "title": info.get("title"),
        "finding": card.finding,
        "design": card.design,
        "sample": card.sample,
        "effect_direction": card.effect_direction,
        "claim_strength": card.claim_strength,
        "limitations": card.limitations,
    }


class Writer:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    async def _section(self, payload: dict[str, Any]) -> SectionDraft:
        user = "Write this section.\n" + embed_input(payload)
        return await self.llm.complete_json(SECTION_SYSTEM, user, SectionDraft, model=self.settings.LLM_WRITING_MODEL)

    async def write(
        self,
        *,
        central_question: str,
        interpretation_note: str | None,
        sub_questions: Sequence[SubQuestionInfo],
        cards: Sequence[Any],
        work_meta: dict[str, dict[str, Any]],
        length: str,
        progress: ProgressFn | None = None,
    ) -> tuple[list[DraftSection], FrontMatter]:
        total = WORD_TARGETS.get(length, WORD_TARGETS["STANDARD"])
        by_sq: dict[str, list[dict[str, Any]]] = {}
        for card in cards:
            by_sq.setdefault(card.sub_question_id, []).append(_card_payload(card, work_meta))
        all_cards = [c for group in by_sq.values() for c in group]
        core = [sq for sq in sub_questions if sq.role == SubQuestionRole.CORE]
        background = [sq for sq in sub_questions if sq.role == SubQuestionRole.BACKGROUND]
        discussion = [sq for sq in sub_questions if sq.role == SubQuestionRole.DISCUSSION]
        body_words = int(total * SHARES["body"] / max(1, len(core)))
        common = {"centralQuestion": central_question, "interpretationNote": interpretation_note}

        body: list[DraftSection] = []
        for i, sq in enumerate(core, start=1):
            if progress:
                await progress(f"Writing body section {i} of {len(core)}")
            draft = await self._section(
                {
                    **common,
                    "section": "body",
                    "subQuestion": sq.question,
                    "wordTarget": body_words,
                    "evidenceCards": by_sq.get(sq.id, []),
                }
            )
            body.append(DraftSection("body", draft.heading, sq.id, draft.content))

        outline = [{"heading": s.heading, "subQuestion": next(q.question for q in core if q.id == s.sub_question_id)} for s in body]
        bg_cards = [c for sq in background for c in by_sq.get(sq.id, [])] or all_cards[:8]
        if progress:
            await progress("Writing introduction")
        intro = await self._section(
            {
                **common,
                "section": "introduction",
                "instructions": "Introduce the topic and its importance, give background, state the central "
                "question, and preview the body sections in the outline.",
                "backgroundQuestions": [sq.question for sq in background],
                "outline": outline,
                "wordTarget": int(total * SHARES["introduction"]),
                "evidenceCards": bg_cards,
            }
        )
        body_text = [{"heading": s.heading, "content": s.content} for s in body]
        if progress:
            await progress("Writing discussion")
        disc = await self._section(
            {
                **common,
                "section": "discussion",
                "instructions": "Synthesize what the body sections show about the central question, compare "
                "findings, address the discussion questions, and note implications. Cite cards for claims.",
                "discussionQuestions": [sq.question for sq in discussion],
                "bodySections": body_text,
                "wordTarget": int(total * SHARES["discussion"]),
                "evidenceCards": list({c["work_id"]: c for c in all_cards}.values()),
            }
        )
        limitations_cards = [c for c in all_cards if c.get("limitations")]
        lim = await self._section(
            {
                **common,
                "section": "limitations",
                "instructions": "Describe limitations of the evidence (designs, samples, measures) using the "
                "cards' limitations, and of this review (single database: OpenAlex; abstracts only; "
                "automated screening). Heading must be 'Limitations'.",
                "wordTarget": int(total * SHARES["limitations"]),
                "evidenceCards": limitations_cards or all_cards,
            }
        )
        if progress:
            await progress("Writing conclusion")
        concl = await self._section(
            {
                **common,
                "section": "conclusion",
                "instructions": "Answer the central question directly and briefly, with the strength of evidence "
                "stated plainly. Heading must be 'Conclusion'.",
                "bodySections": body_text,
                "wordTarget": int(total * SHARES["conclusion"]),
                "evidenceCards": all_cards,
            }
        )
        sections = [
            DraftSection("introduction", "Introduction", None, intro.content),
            *body,
            DraftSection("discussion", "Discussion", None, disc.content),
            DraftSection("limitations", "Limitations", None, lim.content),
            DraftSection("conclusion", "Conclusion", None, concl.content),
        ]
        front = await self.llm.complete_json(
            FRONT_SYSTEM,
            "Write the front matter.\n"
            + embed_input({"centralQuestion": central_question, "sections": [{"heading": s.heading, "content": s.content} for s in sections]}),
            FrontMatter,
            model=self.settings.LLM_WRITING_MODEL,
        )
        front = front.model_copy(update={"abstract": PLACEHOLDER.sub("", front.abstract).replace("  ", " ").strip()})
        return sections, front
