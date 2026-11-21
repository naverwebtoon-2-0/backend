"""CLOVA Dubbing 추상화 (api.md §14: 실제 사양 미확정 → 인터페이스 + Fake 기본 구현)."""

import io
import wave
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings
from app.integrations.http import UpstreamError


@dataclass
class AudioResult:
    data: bytes
    content_type: str
    duration_ms: int


class DubbingClient(Protocol):
    def synthesize(self, text: str, voice_id: str, params: dict, emotion: str) -> AudioResult: ...
    def synthesize_sfx(self, tag: str) -> AudioResult: ...
    def list_voices(self) -> list[dict]: ...
    def list_sfx_presets(self) -> list[dict]: ...


VOICES = [
    {"id": "nara", "name": "차분한 내레이터", "gender": "female", "age": "adult", "sample_url": None},
    {"id": "dain", "name": "차분한 여성 음성", "gender": "female", "age": "young", "sample_url": None},
    {"id": "mijin", "name": "또렷한 여성 음성", "gender": "female", "age": "adult", "sample_url": None},
    {"id": "jinho", "name": "밝은 남성 음성", "gender": "male", "age": "young", "sample_url": None},
    {"id": "minsu", "name": "낮은 남성 음성", "gender": "male", "age": "adult", "sample_url": None},
]
SFX_PRESETS = [
    {
        "tag": "rain_loop",
        "name": "빗소리 · 반복",
        "category": "weather",
        "sample_url": None,
        "duration_ms": 9000,
    },
    {
        "tag": "thunder_near",
        "name": "천둥(가까이)",
        "category": "weather",
        "sample_url": None,
        "duration_ms": 2500,
    },
    {"tag": "door_open", "name": "문 열림", "category": "object", "sample_url": None, "duration_ms": 900},
    {"tag": "footsteps", "name": "발소리", "category": "human", "sample_url": None, "duration_ms": 1500},
    {"tag": "impact", "name": "충돌음", "category": "action", "sample_url": None, "duration_ms": 700},
]
DEFAULT_NARRATION_VOICE = "nara"
DEFAULT_CHARACTER_VOICE = "dain"


def silent_wav(duration_ms: int, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * (sample_rate * duration_ms // 1000))
    return buf.getvalue()


class FakeDubbingClient:
    """짧은 무음 WAV. 텍스트에 FAIL_MARKER가 들어 있으면 실패해 부분 실패를 재현한다."""

    FAIL_MARKER = "[[fail]]"

    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, text: str, voice_id: str, params: dict, emotion: str) -> AudioResult:
        self.calls += 1
        if self.FAIL_MARKER in text:
            raise UpstreamError("clova_dubbing", "synthesis failed", retryable=False)
        duration = min(15_000, 300 + 120 * len(text))
        return AudioResult(silent_wav(duration), "audio/wav", duration)

    def synthesize_sfx(self, tag: str) -> AudioResult:
        preset = next((p for p in SFX_PRESETS if p["tag"] == tag), None)
        if preset is None:
            raise UpstreamError("clova_dubbing", f"unknown sfx tag {tag}", retryable=False)
        duration = int(preset["duration_ms"])
        return AudioResult(silent_wav(duration), "audio/wav", duration)

    def list_voices(self) -> list[dict]:
        return VOICES

    def list_sfx_presets(self) -> list[dict]:
        return [{k: v for k, v in p.items() if k != "duration_ms"} for p in SFX_PRESETS]


class ClovaDubbingClient:
    """CLOVA Dubbing 실제 클라이언트 스켈레톤.

    공개 API 사양(엔드포인트·인증·감정/속도 파라미터·효과음 프리셋)이 확정되지 않아 호출부는 비워 둔다.
    사양 확정 후 synthesize / synthesize_sfx를 구현한다 (docs/open-questions.md 참고).
    """

    def __init__(self, settings: Settings) -> None:
        self.url = settings.clova_dubbing_url
        self.client_id = settings.clova_dubbing_client_id
        self.client_secret = settings.clova_dubbing_client_secret

    def synthesize(self, text: str, voice_id: str, params: dict, emotion: str) -> AudioResult:
        raise UpstreamError("clova_dubbing", "not implemented", retryable=False)

    def synthesize_sfx(self, tag: str) -> AudioResult:
        raise UpstreamError("clova_dubbing", "not implemented", retryable=False)

    def list_voices(self) -> list[dict]:
        return VOICES

    def list_sfx_presets(self) -> list[dict]:
        return [{k: v for k, v in p.items() if k != "duration_ms"} for p in SFX_PRESETS]
