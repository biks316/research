from typing import ClassVar

from pydantic import Field

from app.models.enums import SubQuestionRole
from app.schemas.base import RequestModel, ResponseModel


class SubQuestion(ResponseModel):
    omit_when_none: ClassVar[frozenset[str]] = frozenset({"rationale"})

    id: str
    order: int = Field(ge=1)
    question: str
    rationale: str | None = Field(default=None, description="Why this sub-question is needed to answer the central question.")
    role: SubQuestionRole = Field(
        description="Where it appears: introduction background, a body section, or the discussion."
    )


class Plan(ResponseModel):
    central_question: str
    interpretation_note: str | None = Field(
        default=None, description="How the topic was interpreted or narrowed, shown to the user for confirmation."
    )
    sub_questions: list[SubQuestion] = Field(min_length=1, max_length=8)
    version: int = Field(ge=1, description="Incremented on each edit; used for optimistic locking.")
    editable: bool = Field(description="True only while status is AWAITING_APPROVAL.")


class SubQuestionInput(RequestModel):
    id: str = Field(default=None, description="Omit for new sub-questions; include to keep an existing one.")  # type: ignore[assignment]
    question: str = Field(min_length=10, max_length=500)
    rationale: str = Field(default=None, max_length=1000)  # type: ignore[assignment]
    role: SubQuestionRole


class PlanUpdateRequest(RequestModel):
    version: int = Field(ge=1, description="The plan version this edit is based on.")
    central_question: str = Field(min_length=10, max_length=500)
    sub_questions: list[SubQuestionInput] = Field(
        min_length=1, max_length=8, description="Full ordered list; array order becomes the section order."
    )


class PlanApprovalRequest(RequestModel):
    version: int = Field(ge=1, description="The plan version being approved; must match the current version.")
