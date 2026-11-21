"""외부 AI 호출 공통: 타임아웃, 지수 백오프 재시도, AI_UPSTREAM_ERROR 매핑."""

import logging
import random
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """CLOVA 호출 실패. API 계층에서는 502 AI_UPSTREAM_ERROR로 매핑한다."""

    def __init__(self, service: str, message: str, retryable: bool = True) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.retryable = retryable


def with_retries[T](service: str, fn: Callable[[], T], max_retries: int, base_delay: float = 0.5) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retryable = status == 429 or status >= 500
            error = UpstreamError(service, f"HTTP {status}", retryable)
        except httpx.TransportError as exc:
            error = UpstreamError(service, type(exc).__name__, True)
        if not error.retryable or attempt >= max_retries:
            raise error
        delay = base_delay * (2**attempt) + random.uniform(0, base_delay)  # noqa: S311
        logger.warning("upstream retry", extra={"service": service, "attempt": attempt + 1})
        time.sleep(delay)
        attempt += 1
