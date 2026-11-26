"""외부 AI 호출 공통: 타임아웃, 지수 백오프 재시도, AI_UPSTREAM_ERROR 매핑."""

import logging
import random
import re
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)
_DIAGNOSTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SENSITIVE_TEXT = re.compile(
    r"https?://\S+|\bBearer\s+\S+|\b(?:secret|api[-_ ]?key|access[-_ ]?token)\s*[:=]\s*\S+"
    r"|[A-Za-z0-9_+/=.-]{40,}",
    re.IGNORECASE,
)


def diagnostic_id(value: object) -> str | None:
    if isinstance(value, (str, int)) and _DIAGNOSTIC_ID.fullmatch(str(value)):
        return str(value)
    return None


def diagnostic_message(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _SENSITIVE_TEXT.sub("[가림]", " ".join(value.split()))[:500]


def provider_error(body: object) -> tuple[str | None, str | None]:
    if not isinstance(body, dict):
        return None, None
    for envelope in (body.get("error"), body.get("status"), body):
        if isinstance(envelope, dict):
            code = diagnostic_id(envelope.get("errorCode") or envelope.get("code"))
            message = diagnostic_message(
                envelope.get("errorMsg") or envelope.get("errorMessage") or envelope.get("message")
            )
            detail = diagnostic_message(envelope.get("details"))
            if detail and detail != message:
                message = f"{message}. {detail}" if message else detail
            if code or message:
                return code, message
    return None, None


class UpstreamError(Exception):
    """CLOVA 호출 실패. API 계층에서는 502 AI_UPSTREAM_ERROR로 매핑한다."""

    def __init__(
        self,
        service: str,
        message: str,
        retryable: bool = True,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        self.message = diagnostic_message(message) or "외부 서비스 응답을 확인할 수 없습니다."
        super().__init__(f"{service}: {self.message}")
        self.service = service
        self.retryable = retryable
        self.status = status
        self.code = diagnostic_id(code)
        self.request_id = diagnostic_id(request_id)
        self.operation = operation

    @property
    def app_code(self) -> str:
        return "STORAGE_UPSTREAM_ERROR" if self.service == "object_storage" else "AI_UPSTREAM_ERROR"

    def details(self) -> dict:
        return {
            "service": self.service,
            "upstream": {
                "status": self.status,
                "code": self.code,
                "message": self.message,
                "request_id": self.request_id,
                "retryable": self.retryable,
                "operation": self.operation,
            },
        }


def response_request_id(response: httpx.Response) -> str | None:
    return next(
        (
            value
            for name in ("x-ncp-apigw-request-id", "x-ncp-clovastudio-request-id", "x-request-id")
            if (value := diagnostic_id(response.headers.get(name)))
        ),
        None,
    )


def json_response(service: str, response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise UpstreamError(
            service,
            "외부 서비스 응답이 JSON 객체가 아닙니다.",
            retryable=False,
            status=response.status_code,
            code="INVALID_RESPONSE",
            request_id=response_request_id(response),
        )
    return body


def with_retries[T](service: str, fn: Callable[[], T], max_retries: int, base_delay: float = 0.5) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            response = exc.response
            status = response.status_code
            retryable = status == 429 or status >= 500
            try:
                body = response.json()
            except ValueError:
                body = None
            code, message = provider_error(body)
            error = UpstreamError(
                service,
                message or f"HTTP {status} {response.reason_phrase}",
                retryable,
                status=status,
                code=code,
                request_id=response_request_id(response)
                or (diagnostic_id(body.get("requestId")) if isinstance(body, dict) else None),
            )
        except httpx.TransportError as exc:
            error = UpstreamError(service, type(exc).__name__, True, code=type(exc).__name__)
        if not error.retryable or attempt >= max_retries:
            raise error
        delay = base_delay * (2**attempt) + random.uniform(0, base_delay)  # noqa: S311
        logger.warning("upstream retry", extra={"service": service, "attempt": attempt + 1})
        time.sleep(delay)
        attempt += 1
