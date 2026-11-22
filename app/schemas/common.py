from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """요청 바디: 알 수 없는 필드는 거부한다."""

    model_config = ConfigDict(extra="forbid")


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409)
}
