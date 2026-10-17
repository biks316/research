"""GET /reports/{reportId}/status and /events (Server-Sent Events)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, Reports, SettingsDep
from app.core.logging import bind_report
from app.models.enums import STREAM_END_STATUSES
from app.schemas import ProgressEvent, ReportStatusResponse

router = APIRouter(tags=["Progress"])
log = structlog.get_logger(__name__)

POLL_SLICE_SECONDS = 1.0


def format_sse(event: ProgressEvent) -> str:
    data = json.dumps(event.model_dump(mode="json", by_alias=True), separators=(",", ":"))
    return f"event: progress\ndata: {data}\n\n"


@router.get(
    "/reports/{reportId}/status",
    operation_id="getReportStatus",
    name="getReportStatus",
    response_model=ReportStatusResponse,
    summary="Get current status and progress",
)
async def getReportStatus(reportId: uuid.UUID, user: CurrentUser, reports: Reports) -> ReportStatusResponse:  # noqa: N802, N803
    bind_report(reportId)
    return await reports.get_status(user.user_id, reportId)


@router.get(
    "/reports/{reportId}/events",
    operation_id="streamReportEvents",
    name="streamReportEvents",
    response_class=StreamingResponse,
    summary="Stream live progress events (Server-Sent Events)",
)
async def streamReportEvents(  # noqa: N802
    reportId: uuid.UUID,  # noqa: N803
    request: Request,
    user: CurrentUser,
    reports: Reports,
    settings: SettingsDep,
) -> StreamingResponse:
    bind_report(reportId)
    await reports.ensure_owned(user.user_id, reportId)
    keepalive = settings.SSE_KEEPALIVE_SECONDS

    async def events() -> AsyncIterator[str]:
        # Subscribe before reading the current state so no event slips between the two.
        async with reports.progress.subscribe(reportId) as subscription:
            current = await reports.current_event(reportId)
            if current is None:
                return
            last_event = format_sse(current)
            yield last_event
            if current.status in STREAM_END_STATUSES:
                return
            loop = asyncio.get_running_loop()
            last_sent = loop.time()
            while True:
                if await request.is_disconnected():
                    log.info("sse_client_disconnected")
                    return
                event = await subscription.next_event(timeout=min(POLL_SLICE_SECONDS, keepalive))
                if event is not None:
                    frame = format_sse(event)
                    if frame != last_event:  # the initial read can race the first publish
                        yield frame
                        last_event = frame
                        last_sent = loop.time()
                    if event.status in STREAM_END_STATUSES:
                        return
                elif loop.time() - last_sent >= keepalive:
                    yield ": keep-alive\n\n"
                    last_sent = loop.time()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
