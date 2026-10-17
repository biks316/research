from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from pydantic import Field, model_validator

from app.models.enums import ReportStatus
from app.schemas.base import RequestModel, ResponseModel


class ReportLength(StrEnum):
    SHORT = "SHORT"
    STANDARD = "STANDARD"
    LONG = "LONG"


class CitationStyle(StrEnum):
    APA7 = "APA7"


class ReportOptions(RequestModel):
    year_from: int = Field(default=None, ge=1900, description="Earliest publication year to include.")  # type: ignore[assignment]
    year_to: int = Field(  # type: ignore[assignment]
        default=None, ge=1900, description="Latest publication year to include. Defaults to the current year."
    )
    length: ReportLength = Field(
        default=ReportLength.STANDARD,
        description="Approximate length: SHORT ~1,500 words, STANDARD ~3,000, LONG ~5,000.",
    )
    open_access_only: bool = False
    language: str = Field(default="en", description="ISO 639-1 language code for included sources.")
    citation_style: CitationStyle = CitationStyle.APA7

    @model_validator(mode="after")
    def _year_range(self) -> "ReportOptions":
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("yearFrom must be less than or equal to yearTo")
        return self


class ReportCreateRequest(RequestModel):
    topic: str = Field(min_length=3, max_length=300)
    description: str = Field(
        min_length=10,
        max_length=4000,
        description="What the user wants to know, audience, and any focus or constraints.",
    )
    options: ReportOptions = Field(default=None)  # type: ignore[assignment]


class ReportAccepted(ResponseModel):
    omit_when_none: ClassVar[frozenset[str]] = frozenset({"created_at"})

    id: UUID
    status: ReportStatus
    status_url: str
    created_at: datetime | None = None


class ErrorInfo(ResponseModel):
    code: str
    message: str


class ReportStatusResponse(ResponseModel):
    id: UUID
    status: ReportStatus
    progress_percent: int = Field(ge=0, le=100)
    message: str | None = Field(default=None, description="Human-readable description of the current step.")
    updated_at: datetime
    error: ErrorInfo | None = Field(default=None, description="Present only when status is FAILED.")


class ProgressEvent(ResponseModel):
    """Payload of each Server-Sent Event on /reports/{reportId}/events."""

    omit_when_none: ClassVar[frozenset[str]] = frozenset({"message"})

    status: ReportStatus
    progress_percent: int = Field(ge=0, le=100)
    message: str | None = None
    timestamp: datetime


class ReportSummary(ResponseModel):
    id: UUID
    topic: str
    title: str | None = Field(default=None, description="Report title, available once generated.")
    status: ReportStatus
    created_at: datetime
    updated_at: datetime


class ReportPage(ResponseModel):
    items: list[ReportSummary]
    next_cursor: str | None = Field(
        default=None, description="Pass as `cursor` to get the next page; null when there are no more."
    )
