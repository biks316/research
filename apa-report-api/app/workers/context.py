"""Shared state and error types for worker jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.clients.llm import LLMClient
from app.clients.openalex import OpenAlexClient
from app.config import Settings
from app.models.enums import ErrorCode
from app.services.report_service import ReportService


class JobOutcome(StrEnum):
    COMPLETED = "completed"  # stage work done (AWAITING_APPROVAL or COMPLETED)
    SKIPPED = "skipped"  # already claimed by a live worker, or already finished
    CANCELLED = "cancelled"  # report cancelled or deleted while running
    FAILED = "failed"  # permanent failure; report set to FAILED


class PipelineError(Exception):
    """A permanent job failure with a client-visible error code."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class JobContext:
    settings: Settings
    reports: ReportService
    llm: LLMClient
    openalex: OpenAlexClient
    worker_id: str
