from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """요청 바디: 알 수 없는 필드는 거부한다."""

    model_config = ConfigDict(extra="forbid")


class UpstreamDetails(BaseModel):
    status: int | None = None
    code: str | None = None
    message: str
    request_id: str | None = None
    retryable: bool
    operation: str | None = None


class ErrorDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    reason: str | None = None
    request_id: str | None = None
    service: str | None = None
    upstream: UpstreamDetails | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: ErrorDetails | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 500, 502)
}
