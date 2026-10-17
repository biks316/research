"""POST/GET /reports, GET/DELETE /reports/{reportId}. Handler names match the spec's operationIds."""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, Query, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, Idempotency, Limiter, Reports, ResourcesDep
from app.core.logging import bind_report
from app.models.enums import ReportStatus
from app.schemas import ReportAccepted, ReportCreateRequest, ReportPage
from app.schemas.output import Report
from app.services.idempotency import StoredResponse, body_hash

router = APIRouter(tags=["Reports"])
log = structlog.get_logger(__name__)


@router.post(
    "/reports",
    operation_id="createReport",
    name="createReport",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReportAccepted,
    summary="Submit a topic and description to start a report",
)
async def createReport(  # noqa: N802 - named after the spec's operationId
    body: ReportCreateRequest,
    user: CurrentUser,
    reports: Reports,
    idempotency: Idempotency,
    limiter: Limiter,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JSONResponse:
    request_hash = body_hash(body.model_dump(mode="json", by_alias=True, exclude_unset=True))
    if idempotency_key:
        replay = await idempotency.begin(user.user_id, idempotency_key, request_hash)
        if replay is not None:
            log.info("idempotent_replay", report_id=replay.body.get("id"))
            return JSONResponse(replay.body, status_code=replay.status_code, headers=replay.headers)
    try:
        await limiter.hit(user.user_id, "create_report")
        result = await reports.create_report(user.user_id, body)
    except BaseException:
        if idempotency_key:
            await idempotency.release(user.user_id, idempotency_key)
        raise
    bind_report(result.accepted.id)
    payload = result.accepted.model_dump(mode="json", by_alias=True)
    headers = {"Location": result.location}
    if idempotency_key:
        await idempotency.complete(
            user.user_id, idempotency_key, request_hash, StoredResponse(status.HTTP_202_ACCEPTED, payload, headers)
        )
    return JSONResponse(payload, status_code=status.HTTP_202_ACCEPTED, headers=headers)


@router.get(
    "/reports",
    operation_id="listReports",
    name="listReports",
    response_model=ReportPage,
    summary="List the current user's reports",
)
async def listReports(  # noqa: N802
    user: CurrentUser,
    reports: Reports,
    status_filter: Annotated[ReportStatus | None, Query(alias="status", description="Filter by status.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous response's `nextCursor`.")] = None,
) -> ReportPage:
    return await reports.list_reports(user.user_id, status=status_filter, limit=limit, cursor=cursor)


@router.get(
    "/reports/{reportId}",
    operation_id="getReport",
    name="getReport",
    response_model=Report,
    summary="Get the full report",
)
async def getReport(reportId: uuid.UUID, user: CurrentUser, reports: Reports) -> Report:  # noqa: N802, N803
    bind_report(reportId)
    return await reports.get_report(user.user_id, reportId)


@router.delete(
    "/reports/{reportId}",
    operation_id="deleteReport",
    name="deleteReport",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a report",
)
async def deleteReport(  # noqa: N802
    reportId: uuid.UUID,  # noqa: N803
    user: CurrentUser,
    reports: Reports,
    resources: ResourcesDep,
) -> Response:
    bind_report(reportId)
    # Cancel first so workers stop at their next stage boundary, then remove everything.
    await reports.cancel_for_delete(user.user_id, reportId)
    removed = await resources.storage.delete_prefix(f"exports/{reportId}/")
    await reports.delete_report_rows(reportId)
    log.info("report_deleted", s3_objects_removed=removed)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
