"""Structured LLM outputs used by the pipeline stages.

Every field is required in the JSON schema sent to the model; "unknown" is expressed as null.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SubQuestionRole


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- planner
class PlannedSubQuestion(_Out):
    question: str = Field(min_length=10, max_length=500)
    rationale: str = Field(max_length=1000)
    role: SubQuestionRole


class PlanDraft(_Out):
    central_question: str = Field(min_length=10, max_length=500)
    interpretation_note: str = Field(description="How the topic was understood or narrowed.")
    hidden_assumptions: list[str] = Field(
        description="Assumptions in the user's wording (e.g. causal claims) that the plan tests rather than accepts."
    )
    sub_questions: list[PlannedSubQuestion] = Field(min_length=3, max_length=6)


# ------------------------------------------------------------- retriever
class SearchTerms(_Out):
    boolean_query: str = Field(description="OpenAlex keyword query using AND / OR / NOT and quoted phrases.")
    semantic_query: str = Field(description="One or two natural-language sentences describing relevant papers.")


# -------------------------------------------------------------- screener
class ScreeningScore(_Out):
    work_id: str
    score: int = Field(ge=0, le=3)
    reason: str = Field(max_length=300)


class ScreeningBatch(_Out):
    results: list[ScreeningScore]


# ------------------------------------------------------------- extractor
class ClaimStrength(StrEnum):
    causal = "causal"
    predictive = "predictive"
    associational = "associational"
    descriptive = "descriptive"


class EffectDirection(StrEnum):
    positive = "positive"
    negative = "negative"
    null = "null"
    mixed = "mixed"


class EvidenceCardOut(_Out):
    work_id: str
    finding: str
    design: str | None
    sample: str | None
    effect_direction: EffectDirection | None
    claim_strength: ClaimStrength | None
    limitations: str | None


class EvidenceBatch(_Out):
    cards: list[EvidenceCardOut]


# ---------------------------------------------------------------- writer
class SectionDraft(_Out):
    heading: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class FrontMatter(_Out):
    title: str = Field(min_length=5, max_length=250)
    abstract: str = Field(min_length=50)
    keywords: list[str] = Field(min_length=3, max_length=8)


# -------------------------------------------------------------- verifier
class SentenceVerdict(_Out):
    index: int
    supported: bool
    rewrite: str | None = Field(description="A supported rewrite keeping valid [W…] citations, or null to drop.")


class ClaimCheck(_Out):
    verdicts: list[SentenceVerdict]
