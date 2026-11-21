"""음성·효과음 생성 추상화.

api.md는 CLOVA Dubbing을 가정했지만, CLOVA Dubbing은 콘텐츠 제작용 웹 서비스이며
실시간 API 호출을 지원하지 않는다 (NCP 안내: API 연동은 CLOVA Voice 사용).
그래서 실제 구현은 CLOVA Voice Premium TTS API로 음성을 만들고, 효과음은 Object Storage
프리셋 음원(없으면 내장 생성기)을 쓴다. 인터페이스 이름은 명세에 맞춰 DubbingClient로 둔다.
"""

import io
import logging
import re
import wave
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings
from app.integrations import sfx_synth
from app.integrations.http import UpstreamError, with_retries

logger = logging.getLogger(__name__)


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


# CLOVA Voice Premium 화자 id를 그대로 voice_id로 쓴다.
VOICES = [
    {"id": "nara", "name": "나라 (차분한 내레이터)", "gender": "female", "age": "adult", "sample_url": None},
    {"id": "vdain", "name": "다인 Pro", "gender": "female", "age": "young", "sample_url": None},
    {"id": "vara", "name": "아라 Pro", "gender": "female", "age": "young", "sample_url": None},
    {"id": "vyuna", "name": "유나 Pro", "gender": "female", "age": "young", "sample_url": None},
    {"id": "vgoeun", "name": "고은 Pro", "gender": "female", "age": "adult", "sample_url": None},
    {"id": "vmikyung", "name": "미경 Pro", "gender": "female", "age": "adult", "sample_url": None},
    {"id": "vhyeri", "name": "혜리 Pro", "gender": "female", "age": "young", "sample_url": None},
    {"id": "vdaeseong", "name": "대성 Pro", "gender": "male", "age": "adult", "sample_url": None},
    {"id": "vdonghyun", "name": "동현 Pro", "gender": "male", "age": "young", "sample_url": None},
    {"id": "vian", "name": "이안 Pro", "gender": "male", "age": "young", "sample_url": None},
    {"id": "jinho", "name": "진호", "gender": "male", "age": "adult", "sample_url": None},
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
DEFAULT_CHARACTER_VOICE = "vdain"

# CLOVA Voice Premium 감정 지원 (api.ncloud-docs.com, TTS Premium)
EMOTION_SPEAKERS = {"nara", "vara", "vmikyung", "vdain", "vyuna", "vgoeun", "vdaeseong"}
EMOTION_STRENGTH_SPEAKERS = EMOTION_SPEAKERS - {"nara"}
_EMOTION_CODE = {"neutral": 0, "sad": 1, "happy": 2, "angry": 3, "shout": 3}
_RANGES = {
    "speed": (-5, 10),
    "pitch": (-5, 5),
    "volume": (-5, 5),
    "alpha": (-5, 5),
    "emotion_strength": (0, 2),
}
MAX_SENTENCE_CHARS = 200
MAX_TEXT_CHARS = 2000


def _public(presets: list[dict]) -> list[dict]:
    return [{k: v for k, v in p.items() if k != "duration_ms"} for p in presets]


def _clamp(name: str, value: object) -> int | None:
    try:
        number = round(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    low, high = _RANGES[name]
    return max(low, min(high, number))


def voice_form(speaker: str, params: dict, emotion: str) -> dict[str, str]:
    """명세의 voice_params·emotion을 CLOVA Voice Premium 요청 파라미터로 바꾼다."""
    form: dict[str, str] = {"speaker": speaker, "format": "wav", "sampling-rate": "24000"}
    values = {name: _clamp(name, params[name]) for name in _RANGES if name in params}
    if emotion == "whisper":
        values["volume"] = min(values.get("volume") or 0, -3)
    elif emotion == "shout":
        values["volume"] = max(values.get("volume") or 0, 3)
        values.setdefault("emotion_strength", 2)
    for name in ("speed", "pitch", "volume", "alpha"):
        if values.get(name) is not None:
            form[name] = str(values[name])
    code = _EMOTION_CODE.get(emotion, 0)
    if speaker == "nara" and code == 3:
        code = 0  # nara는 분노(3)를 지원하지 않는다
    if speaker in EMOTION_SPEAKERS and code:
        form["emotion"] = str(code)
        strength = values.get("emotion_strength")
        if speaker in EMOTION_STRENGTH_SPEAKERS and strength is not None:
            form["emotion-strength"] = str(strength)
    return form


def split_sentences(text: str, limit: int = MAX_SENTENCE_CHARS) -> list[str]:
    """문장당 200자 제한(VS18)에 맞춰 나눈다. 문장 경계를 우선하고, 긴 문장은 공백·글자 단위로 자른다."""
    pieces = [p.strip() for p in re.split(r"(?<=[.!?。！？…\n])\s*", text) if p.strip()]
    chunks: list[str] = []
    for piece in pieces:
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            chunks.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            chunks.append(piece)
    return chunks


def concat_wavs(parts: list[bytes]) -> tuple[bytes, int]:
    """같은 형식의 WAV를 이어 붙인다. (wav 바이트, 길이 ms)"""
    params = None
    frames: list[bytes] = []
    for part in parts:
        with wave.open(io.BytesIO(part), "rb") as w:
            if params is None:
                params = w.getparams()
            elif (w.getnchannels(), w.getsampwidth(), w.getframerate()) != params[:3]:
                raise UpstreamError("clova_voice", "inconsistent wav format", retryable=False)
            frames.append(w.readframes(w.getnframes()))
    if params is None:
        raise UpstreamError("clova_voice", "empty audio", retryable=False)
    data = b"".join(frames)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(params.nchannels)
        out.setsampwidth(params.sampwidth)
        out.setframerate(params.framerate)
        out.writeframes(data)
    frame_count = len(data) // (params.nchannels * params.sampwidth)
    return buf.getvalue(), round(frame_count * 1000 / params.framerate)


def wav_duration_ms(data: bytes) -> int | None:
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return round(w.getnframes() * 1000 / w.getframerate())
    except (wave.Error, EOFError):
        return None


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
            raise UpstreamError("clova_voice", "synthesis failed", retryable=False)
        duration = min(15_000, 300 + 120 * len(text))
        return AudioResult(silent_wav(duration), "audio/wav", duration)

    def synthesize_sfx(self, tag: str) -> AudioResult:
        preset = next((p for p in SFX_PRESETS if p["tag"] == tag), None)
        if preset is None:
            raise UpstreamError("clova_voice", f"unknown sfx tag {tag}", retryable=False)
        duration = int(preset["duration_ms"])
        return AudioResult(silent_wav(duration), "audio/wav", duration)

    def list_voices(self) -> list[dict]:
        return VOICES

    def list_sfx_presets(self) -> list[dict]:
        return _public(SFX_PRESETS)


class ClovaVoiceClient:
    """CLOVA Voice Premium TTS (POST /tts-premium/v1/tts, x-www-form-urlencoded, 응답은 음성 바이너리)."""

    def __init__(
        self, settings: Settings, storage: object | None = None, http: httpx.Client | None = None
    ) -> None:
        self.url = settings.clova_voice_url
        self.client_id = settings.clova_voice_client_id
        self.client_secret = settings.clova_voice_client_secret
        self.timeout = settings.ai_timeout_seconds
        self.max_retries = settings.ai_max_retries
        self.sfx_prefix = settings.sfx_preset_prefix.strip("/")
        self.storage = storage
        self.http = http or httpx.Client(timeout=self.timeout)

    def _tts(self, form: dict[str, str]) -> bytes:
        def call() -> bytes:
            res = self.http.post(
                self.url,
                data=form,
                headers={
                    "X-NCP-APIGW-API-KEY-ID": self.client_id,
                    "X-NCP-APIGW-API-KEY": self.client_secret,
                },
                timeout=self.timeout,
            )
            res.raise_for_status()
            return res.content

        return with_retries("clova_voice", call, self.max_retries)

    def synthesize(self, text: str, voice_id: str, params: dict, emotion: str) -> AudioResult:
        if not self.client_id or not self.client_secret:
            raise UpstreamError("clova_voice", "not configured", retryable=False)
        if len(text) > MAX_TEXT_CHARS:
            raise UpstreamError("clova_voice", "text too long", retryable=False)
        chunks = split_sentences(text)
        if not chunks:
            raise UpstreamError("clova_voice", "empty text", retryable=False)
        base = voice_form(voice_id, params, emotion)
        parts = [self._tts({**base, "text": chunk}) for chunk in chunks]
        data, duration = concat_wavs(parts)
        return AudioResult(data, "audio/wav", duration)

    def synthesize_sfx(self, tag: str) -> AudioResult:
        preset = next((p for p in SFX_PRESETS if p["tag"] == tag), None)
        if preset is None:
            raise UpstreamError("clova_voice", f"unknown sfx tag {tag}", retryable=False)
        if self.storage is not None:
            key = f"{self.sfx_prefix}/{tag}.wav"
            try:
                if self.storage.head(key) is not None:  # type: ignore[attr-defined]
                    data = self.storage.get(key)  # type: ignore[attr-defined]
                    return AudioResult(data, "audio/wav", wav_duration_ms(data) or int(preset["duration_ms"]))
            except Exception:  # 저장소 장애 시 내장 음원으로 대체한다
                logger.warning("sfx preset unavailable", extra={"tag": tag})
        data, duration = sfx_synth.generate(tag)
        return AudioResult(data, "audio/wav", duration)

    def list_voices(self) -> list[dict]:
        return VOICES

    def list_sfx_presets(self) -> list[dict]:
        return _public(SFX_PRESETS)


# 이전 이름 호환
ClovaDubbingClient = ClovaVoiceClient
