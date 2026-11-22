"""UUID v7 (RFC 9562) 문자열 ID. 시간순 정렬이 되므로 커서 페이지네이션에도 쓴다."""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_seq = 0


def uuid7() -> uuid.UUID:
    global _last_ms, _seq
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            # 같은 밀리초 안에서도 단조 증가하도록 12비트 카운터를 쓴다.
            _seq += 1
            if _seq > 0xFFF:
                _last_ms += 1
                _seq = 0
            ms = _last_ms
        else:
            _last_ms = ms
            _seq = int.from_bytes(os.urandom(2), "big") & 0x3FF
        rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
        value = (ms & ((1 << 48) - 1)) << 80
        value |= 0x7 << 76
        value |= _seq << 64
        value |= 0b10 << 62
        value |= rand_b
        return uuid.UUID(int=value)


def new_id() -> str:
    return str(uuid7())
