"""명세 §1 오류 형식: {"error": {"code", "message", "details"}}"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def validation_error(message: str = "요청 값이 올바르지 않습니다.", **details: Any) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", message, details or None)


def unauthorized(message: str = "인증이 필요합니다.") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message, headers={"WWW-Authenticate": "Bearer"})


def forbidden(message: str = "권한이 없습니다.") -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


def not_found(message: str = "리소스를 찾을 수 없습니다.") -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def state_invalid(message: str, **details: Any) -> ApiError:
    return ApiError(409, "EPISODE_STATE_INVALID", message, details or None)


def job_in_progress(job_id: str | None = None) -> ApiError:
    return ApiError(
        409,
        "JOB_IN_PROGRESS",
        "이 회차에 이미 진행 중인 작업이 있습니다.",
        {"job_id": job_id} if job_id else None,
    )


def quota_exceeded(metric: str, used: int, limit: int, requested: int) -> ApiError:
    return ApiError(
        429,
        "QUOTA_EXCEEDED",
        "AI 사용 쿼터를 초과했습니다.",
        {"metric": metric, "used": used, "limit": limit, "requested": requested},
    )


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


_STATUS_CODES = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "FILE_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            _body(exc.code, exc.message, exc.details), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(p) for p in err.get("loc", [])], "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            _body("VALIDATION_ERROR", "요청 값이 올바르지 않습니다.", {"errors": errors}), status_code=400
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) else "요청을 처리하지 못했습니다."
        if exc.status_code == 404:
            message = "리소스를 찾을 수 없습니다."
        return JSONResponse(_body(code, message), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return JSONResponse(_body("INTERNAL_ERROR", "서버 오류가 발생했습니다."), status_code=500)
