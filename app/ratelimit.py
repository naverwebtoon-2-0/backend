"""로그인 시도 제한 (프로세스 메모리 기반, 슬라이딩 윈도). 다중 인스턴스에서는 인스턴스별로 적용된다."""

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def retry_after(self, key: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            hits = self._trim(key, now)
            if len(hits) >= self.max_attempts:
                return max(1, int(hits[0] + self.window - now))
        return None

    def hit(self, key: str) -> None:
        with self._lock:
            self._trim(key, time.monotonic()).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
