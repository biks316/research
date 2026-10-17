from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import Field

from app.models.enums import ReportStatus
from app.schemas.base import ResponseModel


class ReportSection(ResponseModel):
    id: str
    order: int = Field(ge=1)
    heading: str
    sub_question_id: str | None = Field(default=None, description="The sub-question this section answers, if any.")
    content: str = Field(
        description='Section body in Markdown with APA 7 in-text citations, e.g. "(Smith & Lee, 2021)".'
    )


class Methodology(ResponseModel):
    omit_when_none: ClassVar[frozenset[str]] = frozenset({"year_range"})

    database: str
    searched_at: datetime
    year_range: str | None = None
    papers_screened: int = Field(ge=0)
    papers_included: int = Field(ge=0)


class Author(ResponseModel):
    family: str
    given: str | None = None


class Reference(ResponseModel):
    omit_when_none: ClassVar[frozenset[str]] = frozenset({"is_open_access"})

    id: str = Field(description="OpenAlex work ID.")
    apa_citation: str = Field(description="Fully formatted APA 7 reference entry.")
    in_text_citation: str = Field(description='Parenthetical in-text form, e.g. "(Smith et al., 2021)".')
    authors: list[Author]
    year: int | None = None
    title: str
    source: str | None = Field(default=None, description="Journal, book or repository name.")
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = Field(default=None, description="DOI as a URL, e.g. https://doi.org/10.1000/xyz123")
    url: str | None = None
    is_open_access: bool | None = None


class ReferenceList(ResponseModel):
    items: list[Reference]


class Report(ResponseModel):
    omit_when_none: ClassVar[frozenset[str]] = frozenset({"abstract", "keywords"})

    id: UUID
    status: ReportStatus
    title: str
    topic: str
    central_question: str
    abstract: str | None = Field(default=None, description="APA abstract (about 150-250 words).")
    keywords: list[str] | None = None
    sections: list[ReportSection]
    references: list[Reference]
    methodology: Methodology
    created_at: datetime
    completed_at: datetime
