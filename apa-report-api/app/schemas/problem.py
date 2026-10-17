from typing import ClassVar

from pydantic import BaseModel

from app.schemas.base import ResponseModel


class FieldError(BaseModel):
    field: str
    message: str


class Problem(ResponseModel):
    """RFC 9457 Problem Details. The API always fills every field except `errors`."""

    omit_when_none: ClassVar[frozenset[str]] = frozenset({"type", "detail", "instance", "code", "errors"})

    type: str | None = None
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str | None = None
    errors: list[FieldError] | None = None
