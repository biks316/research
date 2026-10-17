"""GET /reports/{reportId}/references and /export."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, Reports
from app.core.logging import bind_report
from app.schemas import ReferenceList
from app.services.export_service import ExportService, content_disposition

router = APIRouter(tags=["Output"])


class ExportFormat(StrEnum):
    docx = "docx"
    pdf = "pdf"


@router.get(
    "/reports/{reportId}/references",
    operation_id="getReferences",
    name="getReferences",
    response_model=ReferenceList,
    summary="Get the report's reference list with paper details",
)
async def getReferences(reportId: uuid.UUID, user: CurrentUser, reports: Reports) -> ReferenceList:  # noqa: N802, N803
    bind_report(reportId)
    return await reports.get_references(user.user_id, reportId)


@router.get(
    "/reports/{reportId}/export",
    operation_id="exportReport",
    name="exportReport",
    response_class=StreamingResponse,
    summary="Download the report as a file",
)
async def exportReport(  # noqa: N802
    reportId: uuid.UUID,  # noqa: N803
    request: Request,
    user: CurrentUser,
    reports: Reports,
    format: Annotated[ExportFormat, Query()],  # noqa: A002 - the spec names the parameter `format`
) -> StreamingResponse:
    bind_report(reportId)
    exporter: ExportService = request.app.state.exporter
    export = await exporter.export(user.user_id, reportId, format.value)
    return StreamingResponse(
        export.chunks,
        media_type=export.media_type,
        headers={
            "Content-Disposition": content_disposition(export.filename),
            "Content-Length": str(export.size),
        },
    )
