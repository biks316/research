"""FastAPI dependencies: settings, services, and the authenticated user."""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.core.errors import Unauthorized
from app.core.resources import Resources
from app.core.security import Principal, TokenVerifier
from app.services.idempotency import IdempotencyStore
from app.services.rate_limit import RateLimiter
from app.services.report_service import ReportService

bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_resources(request: Request) -> Resources:
    return request.app.state.resources


def get_report_service(request: Request) -> ReportService:
    return request.app.state.resources.reports


def get_idempotency(request: Request) -> IdempotencyStore:
    return request.app.state.idempotency


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise Unauthorized()
    verifier: TokenVerifier = request.app.state.verifier
    principal = await verifier.verify(credentials.credentials)
    structlog.contextvars.bind_contextvars(user_id=principal.user_id)
    return principal


CurrentUser = Annotated[Principal, Depends(get_current_user)]
Reports = Annotated[ReportService, Depends(get_report_service)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ResourcesDep = Annotated[Resources, Depends(get_resources)]
Idempotency = Annotated[IdempotencyStore, Depends(get_idempotency)]
Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]
