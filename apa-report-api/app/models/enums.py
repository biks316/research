from enum import StrEnum


class ReportStatus(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    QUEUED_FOR_GENERATION = "QUEUED_FOR_GENERATION"
    SEARCHING = "SEARCHING"
    SCREENING = "SCREENING"
    EXTRACTING = "EXTRACTING"
    WRITING = "WRITING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SubQuestionRole(StrEnum):
    BACKGROUND = "BACKGROUND"
    CORE = "CORE"
    DISCUSSION = "DISCUSSION"


class QueueName(StrEnum):
    PLANNING = "planning"
    GENERATION = "generation"


class JobPhase(StrEnum):
    PLAN = "PLAN"
    GENERATE = "GENERATE"


class ErrorCode(StrEnum):
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    INSUFFICIENT_SOURCES = "INSUFFICIENT_SOURCES"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


TERMINAL_STATUSES = frozenset({ReportStatus.COMPLETED, ReportStatus.FAILED, ReportStatus.CANCELLED})
# States in which a worker owns (or is about to own) the report.
ACTIVE_STATUSES = frozenset(
    {
        ReportStatus.QUEUED,
        ReportStatus.PLANNING,
        ReportStatus.QUEUED_FOR_GENERATION,
        ReportStatus.SEARCHING,
        ReportStatus.SCREENING,
        ReportStatus.EXTRACTING,
        ReportStatus.WRITING,
        ReportStatus.VERIFYING,
    }
)
NON_TERMINAL_STATUSES = ACTIVE_STATUSES | {ReportStatus.AWAITING_APPROVAL}
# Statuses after which an SSE stream closes.
STREAM_END_STATUSES = TERMINAL_STATUSES | {ReportStatus.AWAITING_APPROVAL}
GENERATION_STAGES = (
    ReportStatus.SEARCHING,
    ReportStatus.SCREENING,
    ReportStatus.EXTRACTING,
    ReportStatus.WRITING,
    ReportStatus.VERIFYING,
)
