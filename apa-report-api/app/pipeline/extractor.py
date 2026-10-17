"""Extraction: one evidence card per kept paper."""

from __future__ import annotations

from collections.abc import Sequence

from app.clients.llm import LLMClient, embed_input
from app.clients.openalex import Work
from app.pipeline.models import EvidenceCardOut, EvidenceBatch

BATCH_SIZE = 5
ABSTRACT_CHARS = 3000

SYSTEM = """TASK: extract
You extract evidence from paper abstracts for a literature review. For each paper, write one
evidence card relevant to the sub-question:
- finding: the main result as reported, in one or two sentences, with numbers if given.
- design: study design (e.g. "longitudinal cohort", "randomized controlled trial", "meta-analysis").
- sample: who and how many (e.g. "1,200 US adolescents aged 13-17").
- effect_direction: positive, negative, null, or mixed - for the relationship in the sub-question.
- claim_strength: causal (randomized or strong causal design), predictive (prospective/longitudinal),
  associational (cross-sectional correlation), or descriptive (prevalence, qualitative, description).
- limitations: limitations stated or evident from the design.
Use only what the abstract says. If a value is not stated, use null - never infer or guess it.
Use each work_id exactly as given and return one card per paper. Return only the JSON object."""


async def extract(llm: LLMClient, *, question: str, works: Sequence[Work]) -> list[EvidenceCardOut]:
    cards: list[EvidenceCardOut] = []
    for start in range(0, len(works), BATCH_SIZE):
        batch = works[start : start + BATCH_SIZE]
        user = "Extract evidence cards.\n" + embed_input(
            {
                "subQuestion": question,
                "papers": [
                    {"work_id": w.id, "title": w.title, "year": w.year, "abstract": (w.abstract or "")[:ABSTRACT_CHARS]}
                    for w in batch
                ],
            }
        )
        out = await llm.complete_json(SYSTEM, user, EvidenceBatch)
        wanted = {w.id for w in batch}
        seen: set[str] = set()
        for card in out.cards:
            work_id = card.work_id.strip().upper()
            if work_id in wanted and work_id not in seen and card.finding.strip():
                seen.add(work_id)
                cards.append(card.model_copy(update={"work_id": work_id}))
    return cards
