"""RFC 9457 Problem Details: exception types and FastAPI handlers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.problem import FieldError, Problem

PROBLEM_MEDIA_TYPE = "application/problem+json"
log = structlog.get_logger(__name__)


class ApiError(Exception):
    """An error that is safe to show to the client as a Problem response."""

    status: int = 500
    code: str = "INTERNAL_ERROR"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        title: str | None = None,
        errors: Iterable[FieldError] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.title
        if code:
            self.code = code
        if title:
            self.title = title
        self.errors = list(errors) if errors else None
        self.headers = dict(headers or {})
        super().__init__(self.detail)


class ValidationProblem(ApiError):
    status, code, title = 400, "VALIDATION_ERROR", "Validation failed"


class Unauthorized(ApiError):
    status, code, title = 401, "UNAUTHORIZED", "Unauthorized"

    def __init__(self, detail: str = "Missing or invalid bearer token.") -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"})


class NotFound(ApiError):
    status, code, title = 404, "NOT_FOUND", "Not found"


class ReportNotFound(NotFound):
    code, title = "REPORT_NOT_FOUND", "Report not found"

    def __init__(self) -> None:
        super().__init__("Report not found.")


class Conflict(ApiError):
    status, code, title = 409, "CONFLICT", "Conflict"


class ReportNotReady(Conflict):
    code, title = "REPORT_NOT_READY", "Report not ready"


class InvalidReportStatus(Conflict):
    code, title = "INVALID_REPORT_STATUS", "Operation not allowed in current status"


class PlanVersionConflict(Conflict):
    code, title = "PLAN_VERSION_CONFLICT", "Plan version conflict"


class IdempotencyConflict(Conflict):
    code, title = "IDEMPOTENCY_KEY_REUSED", "Idempotency key reused"


class TooManyRequests(ApiError):
    status, code, title = 429, "RATE_LIMITED", "Too many requests"

    def __init__(self, detail: str, *, retry_after: int, code: str | None = None) -> None:
        super().__init__(detail, code=code, headers={"Retry-After": str(max(1, int(retry_after)))})


def problem_type(base_url: str, code: str) -> str:
    return f"{base_url.rstrip('/')}/{code.lower().replace('_', '-')}"


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    errors: list[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    base_url = request.app.state.settings.PROBLEM_TYPE_BASE_URL
    body = Problem(
        type=problem_type(base_url, code),
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        code=code,
        errors=errors,
    )
    return JSONResponse(
        body.model_dump(mode="json", by_alias=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers or {}),
    )


# --- Validation error formatting --------------------------------------------

_LOCATION_PREFIXES = {"body", "query", "path", "header", "cookie"}


def _field_name(loc: tuple[Any, ...]) -> str:
    parts = list(loc)
    if parts and parts[0] in _LOCATION_PREFIXES:
        parts = parts[1:]
    if not parts:
        return "body" if loc and loc[0] == "body" else str(loc[0]) if loc else "request"
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _friendly_message(err: Mapping[str, Any]) -> str:
    ctx = err.get("ctx") or {}
    kind = err.get("type", "")
    messages = {
        "missing": "is required",
        "extra_forbidden": "is not allowed",
        "string_too_short": f"must be at least {ctx.get('min_length')} characters",
        "string_too_long": f"must be at most {ctx.get('max_length')} characters",
        "too_short": f"must contain at least {ctx.get('min_length')} items",
        "too_long": f"must contain at most {ctx.get('max_length')} items",
        "greater_than_equal": f"must be greater than or equal to {ctx.get('ge')}",
        "less_than_equal": f"must be less than or equal to {ctx.get('le')}",
        "greater_than": f"must be greater than {ctx.get('gt')}",
        "less_than": f"must be less than {ctx.get('lt')}",
        "int_parsing": "must be an integer",
        "int_type": "must be an integer",
        "int_from_float": "must be an integer",
        "bool_parsing": "must be a boolean",
        "bool_type": "must be a boolean",
        "string_type": "must be a string",
        "uuid_parsing": "must be a valid UUID",
        "uuid_type": "must be a valid UUID",
        "json_invalid": "is not valid JSON",
        "model_attributes_type": "must be an object",
        "dict_type": "must be an object",
        "list_type": "must be an array",
    }
    if kind == "enum":
        return f"must be one of: {ctx.get('expected', '')}".replace("'", "")
    if kind in messages:
        return messages[kind]
    if kind == "value_error":
        return str(ctx.get("error", err.get("msg", "is invalid")))
    return str(err.get("msg", "is invalid")).rstrip(".")


def validation_field_errors(errors: Iterable[Mapping[str, Any]]) -> list[FieldError]:
    result: list[FieldError] = []
    for err in errors:
        loc = tuple(err.get("loc", ()))
        # Errors on the model itself (model validators) have no field part.
        result.append(FieldError(field=_field_name(loc), message=_friendly_message(err)))
    return result


def summarize(field_errors: list[FieldError]) -> str:
    first = field_errors[0]
    detail = f"{first.field} {first.message}"
    if len(field_errors) > 1:
        detail += f" (and {len(field_errors) - 1} more)"
    return detail


# --- Handlers ----------------------------------------------------------------


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return problem_response(
        request,
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        errors=exc.errors,
        headers=exc.headers,
    )


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    field_errors = validation_field_errors(exc.errors())
    return problem_response(
        request,
        status=400,
        code=ValidationProblem.code,
        title=ValidationProblem.title,
        detail=summarize(field_errors),
        errors=field_errors,
    )


_HTTP_STATUS_CODES = {
    400: ("BAD_REQUEST", "Bad request"),
    401: ("UNAUTHORIZED", "Unauthorized"),
    403: ("FORBIDDEN", "Forbidden"),
    404: ("NOT_FOUND", "Not found"),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
    406: ("NOT_ACCEPTABLE", "Not acceptable"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "Unsupported media type"),
}


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code, title = _HTTP_STATUS_CODES.get(exc.status_code, ("HTTP_ERROR", "Request failed"))
    detail = exc.detail if isinstance(exc.detail, str) and exc.status_code < 500 else title
    return problem_response(
        request, status=exc.status_code, code=code, title=title, detail=detail, headers=getattr(exc, "headers", None)
    )


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", path=request.url.path)
    return problem_response(
        request,
        status=500,
        code="INTERNAL_ERROR",
        title="Internal server error",
        detail="An unexpected error occurred. Please try again later.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_handler)
