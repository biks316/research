"""GET/PUT /reports/{reportId}/plan and POST /reports/{reportId}/plan/approve."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, Reports
from app.core.logging import bind_report
from app.schemas import Plan, PlanApprovalRequest, PlanUpdateRequest, ReportAccepted

router = APIRouter(tags=["Plan"])


@router.get(
    "/reports/{reportId}/plan",
    operation_id="getPlan",
    name="getPlan",
    response_model=Plan,
    summary="Get the research plan",
)
async def getPlan(reportId: uuid.UUID, user: CurrentUser, reports: Reports) -> Plan:  # noqa: N802, N803
    bind_report(reportId)
    return await reports.get_plan(user.user_id, reportId)


@router.put(
    "/reports/{reportId}/plan",
    operation_id="updatePlan",
    name="updatePlan",
    response_model=Plan,
    summary="Edit the research plan before approval",
)
async def updatePlan(  # noqa: N802
    reportId: uuid.UUID,  # noqa: N803
    body: PlanUpdateRequest,
    user: CurrentUser,
    reports: Reports,
) -> Plan:
    bind_report(reportId)
    return await reports.update_plan(user.user_id, reportId, body)


@router.post(
    "/reports/{reportId}/plan/approve",
    operation_id="approvePlan",
    name="approvePlan",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReportAccepted,
    summary="Approve the plan and start full report generation",
)
async def approvePlan(  # noqa: N802
    reportId: uuid.UUID,  # noqa: N803
    body: PlanApprovalRequest,
    user: CurrentUser,
    reports: Reports,
) -> ReportAccepted:
    bind_report(reportId)
    return await reports.approve_plan(user.user_id, reportId, body)
