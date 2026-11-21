"""효과음 기본 음원 생성기 (외부 의존성 없음).

CLOVA에는 효과음 API가 없으므로, Object Storage에 프리셋 음원(sfx/presets/{tag}.wav)이 없을 때
이 생성기로 만든 기본 효과음을 쓴다. 같은 태그는 항상 같은 음원이 나온다.
"""

import io
import math
import random
import struct
import wave
from collections.abc import Callable

RATE = 22050


def _to_wav(samples: list[float]) -> bytes:
    peak = max((abs(s) for s in samples), default=0.0) or 1.0
    scale = 0.8 * 32767 / peak
    frames = struct.pack(f"<{len(samples)}h", *(int(s * scale) for s in samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)
    return buf.getvalue()


def _lowpass(samples: list[float], alpha: float) -> list[float]:
    out, prev = [], 0.0
    for s in samples:
        prev += alpha * (s - prev)
        out.append(prev)
    return out


def _noise(rng: random.Random, seconds: float) -> list[float]:
    return [rng.uniform(-1, 1) for _ in range(int(RATE * seconds))]


def _rain(rng: random.Random) -> list[float]:
    base = _lowpass(_noise(rng, 9.0), 0.35)
    out = []
    for i, s in enumerate(base):
        drop = 3.0 if rng.random() < 0.0008 else 1.0  # 가끔 굵은 빗방울
        fade = min(1.0, i / RATE, (len(base) - i) / RATE)  # 반복 재생 시 이음매를 줄인다
        out.append(s * drop * fade)
    return out


def _thunder(rng: random.Random) -> list[float]:
    n = int(RATE * 2.5)
    crack = _lowpass(_noise(rng, 2.5), 0.6)
    rumble = _lowpass(_noise(rng, 2.5), 0.02)
    return [
        crack[i] * math.exp(-i / (RATE * 0.08)) + 6 * rumble[i] * math.exp(-i / (RATE * 0.9))
        for i in range(n)
    ]


def _door(rng: random.Random) -> list[float]:
    n = int(RATE * 0.9)
    out, phase = [], 0.0
    for i in range(n):
        t = i / RATE
        freq = 180 + 140 * math.sin(2 * math.pi * 1.7 * t) + rng.uniform(-15, 15)
        phase += freq / RATE
        saw = 2 * (phase % 1.0) - 1
        env = min(1.0, t / 0.05) * min(1.0, (0.9 - t) / 0.15)
        out.append(saw * env * 0.6)
    return _lowpass(out, 0.3)


def _footsteps(rng: random.Random) -> list[float]:
    n = int(RATE * 1.5)
    out = [0.0] * n
    for step in range(4):
        start = int(RATE * (0.05 + step * 0.36))
        thump = _lowpass(_noise(rng, 0.12), 0.08)
        for j, s in enumerate(thump):
            if start + j < n:
                out[start + j] += s * math.exp(-j / (RATE * 0.03))
    return out


def _impact(rng: random.Random) -> list[float]:
    n = int(RATE * 0.7)
    noise = _lowpass(_noise(rng, 0.7), 0.25)
    return [
        noise[i] * math.exp(-i / (RATE * 0.12))
        + 0.5 * math.sin(2 * math.pi * 60 * i / RATE) * math.exp(-i / (RATE * 0.2))
        for i in range(n)
    ]


GENERATORS: dict[str, Callable[[random.Random], list[float]]] = {
    "rain_loop": _rain,
    "thunder_near": _thunder,
    "door_open": _door,
    "footsteps": _footsteps,
    "impact": _impact,
}


def generate(tag: str) -> tuple[bytes, int]:
    """(wav 바이트, 길이 ms). 모르는 태그면 KeyError."""
    samples = GENERATORS[tag](random.Random(tag))  # noqa: S311 - 음원 생성용 결정적 난수
    return _to_wav(samples), round(len(samples) * 1000 / RATE)
