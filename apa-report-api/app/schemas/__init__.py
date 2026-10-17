from app.schemas.output import Author, Methodology, Reference, ReferenceList, Report, ReportSection
from app.schemas.plan import Plan, PlanApprovalRequest, PlanUpdateRequest, SubQuestion, SubQuestionInput
from app.schemas.problem import FieldError, Problem
from app.schemas.reports import (
    ErrorInfo,
    ProgressEvent,
    ReportAccepted,
    ReportCreateRequest,
    ReportOptions,
    ReportPage,
    ReportStatusResponse,
    ReportSummary,
)

# Component name in openapi.yaml -> Pydantic model.
COMPONENT_MODELS = {
    "ReportOptions": ReportOptions,
    "ReportCreateRequest": ReportCreateRequest,
    "ReportAccepted": ReportAccepted,
    "ErrorInfo": ErrorInfo,
    "ReportStatusResponse": ReportStatusResponse,
    "ProgressEvent": ProgressEvent,
    "ReportSummary": ReportSummary,
    "ReportPage": ReportPage,
    "SubQuestion": SubQuestion,
    "Plan": Plan,
    "SubQuestionInput": SubQuestionInput,
    "PlanUpdateRequest": PlanUpdateRequest,
    "PlanApprovalRequest": PlanApprovalRequest,
    "ReportSection": ReportSection,
    "Methodology": Methodology,
    "Report": Report,
    "Author": Author,
    "Reference": Reference,
    "ReferenceList": ReferenceList,
    "Problem": Problem,
}

__all__ = [
    "COMPONENT_MODELS",
    "Author",
    "ErrorInfo",
    "FieldError",
    "Methodology",
    "Plan",
    "PlanApprovalRequest",
    "PlanUpdateRequest",
    "Problem",
    "ProgressEvent",
    "Reference",
    "ReferenceList",
    "Report",
    "ReportAccepted",
    "ReportCreateRequest",
    "ReportOptions",
    "ReportPage",
    "ReportSection",
    "ReportStatusResponse",
    "ReportSummary",
    "SubQuestion",
    "SubQuestionInput",
]
