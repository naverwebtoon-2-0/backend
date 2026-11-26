"""CLOVA OCR 추상화."""

import base64
import re
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

import httpx
from PIL import Image, UnidentifiedImageError

from app.config import Settings
from app.integrations.http import UpstreamError, with_retries

_OCR_LANGUAGES = frozenset({"ko", "ja", "zh-TW"})
_NO_TEXT = re.compile(r'"errorCode"\s*:\s*"ENGN-001"\s*,\s*"errorMsg"\s*:\s*"NO_TEXT"')


@dataclass
class OcrField:
    text: str
    bbox: dict[str, float]  # 상대 좌표 x, y, w, h (0~1)
    confidence: float


class OcrClient(Protocol):
    def recognize(
        self, image: bytes, content_type: str, width: int, height: int, language: str
    ) -> list[OcrField]: ...


class FakeOcrClient:
    """이미지 바이트에 따라 결정적인 결과를 낸다. 이미지마다 나레이션 1개 + 대사 2개 + 효과음 글자 1개."""

    def recognize(
        self, image: bytes, content_type: str, width: int, height: int, language: str
    ) -> list[OcrField]:
        seed = sum(image[:64]) % 7
        return [
            OcrField("어둠 속에서 눈을 떴다.", {"x": 0.1, "y": 0.05, "w": 0.5, "h": 0.04}, 0.98),
            OcrField("여기가 어디지?", {"x": 0.12, "y": 0.231, "w": 0.3, "h": 0.04}, 0.95),
            OcrField("누구야!", {"x": 0.55, "y": 0.5, "w": 0.2, "h": 0.04}, 0.62 if seed == 0 else 0.93),
            OcrField("쾅!", {"x": 0.4, "y": 0.7, "w": 0.15, "h": 0.05}, 0.9),
        ]


class ClovaOcrClient:
    """CLOVA OCR General (V2). invoke URL과 secret은 콘솔의 Domain 설정에서 발급한다."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=settings.ai_timeout_seconds)
        self.url = settings.clova_ocr_invoke_url
        self.secret = settings.clova_ocr_secret
        self.timeout = settings.ai_timeout_seconds
        self.max_retries = settings.ai_max_retries

    def recognize(
        self, image: bytes, content_type: str, width: int, height: int, language: str
    ) -> list[OcrField]:
        if not self.url or not self.secret:
            raise UpstreamError("clova_ocr", "not configured", retryable=False)
        fmt = "jpg" if content_type == "image/jpeg" else "png"
        if content_type == "image/webp":
            try:
                with Image.open(BytesIO(image), formats=["WEBP"]) as source, BytesIO() as output:
                    rgba = source.convert("RGBA")
                    background = Image.new("RGB", source.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    background.save(output, format="JPEG", quality=95)
                    image = output.getvalue()
                    fmt = "jpg"
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise UpstreamError(
                    "clova_ocr", "WebP 이미지를 변환할 수 없습니다.", retryable=False
                ) from exc
        body = {
            "version": "V2",
            "requestId": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
            "images": [{"format": fmt, "name": "page", "data": base64.b64encode(image).decode()}],
        }
        # 영어 등 별도 언어 코드가 없는 경우 도메인 기본 설정으로 인식한다.
        lang = language.strip()
        if lang in _OCR_LANGUAGES:
            body["lang"] = lang

        def call() -> dict:
            res = self.http.post(
                self.url, json=body, headers={"X-OCR-SECRET": self.secret}, timeout=self.timeout
            )
            res.raise_for_status()
            return res.json()

        data = with_retries("clova_ocr", call, self.max_retries)
        result = (data.get("images") or [{}])[0]
        if result.get("inferResult") != "SUCCESS":
            if _NO_TEXT.search(str(result.get("message", ""))):
                return []
            raise UpstreamError("clova_ocr", "이미지 인식에 실패했습니다.", retryable=False)
        fields: list[OcrField] = []
        for field in result.get("fields", []):
            vertices = field.get("boundingPoly", {}).get("vertices", [])
            xs = [v.get("x", 0.0) for v in vertices] or [0.0]
            ys = [v.get("y", 0.0) for v in vertices] or [0.0]
            x0, y0 = max(0.0, min(xs)), max(0.0, min(ys))
            bbox = {
                "x": round(min(1.0, x0 / max(width, 1)), 4),
                "y": round(min(1.0, y0 / max(height, 1)), 4),
                "w": round(min(1.0, (max(xs) - x0) / max(width, 1)), 4),
                "h": round(min(1.0, (max(ys) - y0) / max(height, 1)), 4),
            }
            fields.append(
                OcrField(field.get("inferText", ""), bbox, float(field.get("inferConfidence", 0.0)))
            )
        return fields
