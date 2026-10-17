"""Planning: topic + description -> central question, 3-6 sub-questions, interpretation note."""

from __future__ import annotations

from typing import Any

from app.clients.llm import LLMClient, embed_input
from app.models.enums import SubQuestionRole
from app.pipeline.models import PlanDraft

SYSTEM = """TASK: plan
You are a research methodologist planning a citation-backed APA 7 literature review.

Turn the user's topic and description into a research plan:
- central_question: one precise, answerable research question. Name the population, the
  exposure or phenomenon, and the outcome where they apply. Do not presuppose the answer.
- sub_questions: 3 to 6 questions that together answer the central question, in reading order.
  role BACKGROUND = context for the introduction (at most one or two),
  role CORE = a body section that answers part of the question (at least one),
  role DISCUSSION = interpretation, implications, or what can be done (at most two).
  Each has a short rationale saying why it is needed.
- hidden_assumptions: list every assumption hidden in the user's wording - for example "causes",
  "leads to", "is harmful", or a presupposed direction of effect. Do not accept these as facts:
  for each one, include a sub-question that tests it (e.g. "Is the association causal, or explained
  by confounders or reverse causation?").
- interpretation_note: two or three sentences, addressed to the user, stating how you understood
  and narrowed the topic (scope, population, time frame) and which assumptions the plan will test.

Write in neutral academic English. Return only the JSON object."""


async def plan(llm: LLMClient, *, topic: str, description: str, options: dict[str, Any]) -> PlanDraft:
    user = (
        "Plan a literature review for this request.\n"
        + embed_input(
            {
                "topic": topic,
                "description": description,
                "yearFrom": options.get("yearFrom"),
                "yearTo": options.get("yearTo"),
                "length": options.get("length", "STANDARD"),
                "language": options.get("language", "en"),
            }
        )
    )
    draft = await llm.complete_json(SYSTEM, user, PlanDraft)
    return _normalize(draft)


def _normalize(draft: PlanDraft) -> PlanDraft:
    """Guarantee at least one CORE sub-question and keep background first, discussion last."""
    subs = list(draft.sub_questions)
    if not any(sq.role == SubQuestionRole.CORE for sq in subs):
        middle = 1 if len(subs) > 2 else 0
        subs[middle] = subs[middle].model_copy(update={"role": SubQuestionRole.CORE})
    rank = {SubQuestionRole.BACKGROUND: 0, SubQuestionRole.CORE: 1, SubQuestionRole.DISCUSSION: 2}
    subs.sort(key=lambda sq: rank[sq.role])  # stable: keeps the model's order within a role
    return draft.model_copy(update={"sub_questions": subs})
