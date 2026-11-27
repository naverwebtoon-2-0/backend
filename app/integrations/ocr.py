"""CLOVA OCR 추상화."""

import base64
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

import httpx
from PIL import Image, UnidentifiedImageError

from app.config import Settings
from app.integrations.http import UpstreamError, diagnostic_id, json_response, provider_error, with_retries

_OCR_LANGUAGES = frozenset({"ko", "ja", "zh-TW"})
_MAX_SIDE = 7999
_TILE_OVERLAP = 256


@dataclass
class OcrField:
    text: str
    bbox: dict[str, float]  # 상대 좌표 x, y, w, h (0~1)
    confidence: float
    line_break: bool = False  # CLOVA lineBreak: 이 단어 뒤에서 줄이 바뀜


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
            raise UpstreamError(
                "clova_ocr", "CLOVA OCR 인증 설정이 비어 있습니다.", False, code="NOT_CONFIGURED"
            )
        if 0 < min(width, height) <= max(width, height) <= _MAX_SIDE and content_type != "image/webp":
            fmt = "jpg" if content_type == "image/jpeg" else "png"
            return _parse_fields(self._request(image, fmt, language), width, height)

        fields = []
        try:
            with Image.open(BytesIO(image), formats=("PNG", "JPEG", "WEBP")) as source:
                width, height = source.size
                if max(width, height) <= _MAX_SIDE and source.format != "WEBP":
                    fmt = "jpg" if source.format == "JPEG" else "png"
                    return _parse_fields(self._request(image, fmt, language), width, height)
                for box, owned in _tiles(width, height):
                    with source.crop(box) as tile, BytesIO() as output:
                        _encode_jpeg(tile, output)
                        result = self._request(output.getvalue(), "jpg", language)
                    fields.extend(_parse_fields(result, width, height, box[:2], owned))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise UpstreamError(
                "clova_ocr", "OCR 요청용 이미지를 변환할 수 없습니다.", False, code="IMAGE_CONVERSION_FAILED"
            ) from exc
        return sorted(fields, key=lambda field: (field.bbox["y"], field.bbox["x"]))

    def _request(self, image: bytes, fmt: str, language: str) -> list[dict]:
        request_id = str(uuid.uuid4())
        body = {
            "version": "V2",
            "requestId": request_id,
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
            return json_response("clova_ocr", res)

        data = with_retries("clova_ocr", call, self.max_retries)
        images = data.get("images")
        result = images[0] if isinstance(images, list) and images and isinstance(images[0], dict) else {}
        status = result.get("inferResult")
        fields = result.get("fields", [])
        if status == "SUCCESS" and isinstance(fields, list):
            return fields
        message = result.get("message")
        code, nested_message = _engine_error(message)
        if code == "ENGN-001" and nested_message == "NO_TEXT":
            return []
        response_code, response_message = provider_error(data)
        raise UpstreamError(
            "clova_ocr",
            nested_message or message or response_message or "이미지 인식 결과가 없습니다.",
            False,
            status=200,
            code=code
            or (diagnostic_id(status) if status != "SUCCESS" else None)
            or response_code
            or "INVALID_RESPONSE",
            request_id=diagnostic_id(data.get("requestId")) or request_id,
        )


def _engine_error(message: object) -> tuple[str | None, str | None]:
    if isinstance(message, str) and (start := message.find("{")) >= 0:
        try:
            body, _ = json.JSONDecoder().raw_decode(message[start:])
            return provider_error(body)
        except ValueError:
            pass
    return None, None


def _axis_segments(length: int) -> list[tuple[int, int]]:
    starts = [0]
    while starts[-1] + _MAX_SIDE < length:
        starts.append(starts[-1] + _MAX_SIDE - _TILE_OVERLAP)
    return [(start, min(start + _MAX_SIDE, length)) for start in starts]


def _tiles(
    width: int, height: int
) -> Iterator[tuple[tuple[int, int, int, int], tuple[float, float, float, float]]]:
    for top, bottom in _axis_segments(height):
        for left, right in _axis_segments(width):
            # 겹친 영역은 중심점을 기준으로 한 조각에서만 채택한다.
            owned = (
                left + (_TILE_OVERLAP / 2 if left else 0),
                top + (_TILE_OVERLAP / 2 if top else 0),
                right - (_TILE_OVERLAP / 2 if right < width else 0),
                bottom - (_TILE_OVERLAP / 2 if bottom < height else 0),
            )
            yield (left, top, right, bottom), owned


def _encode_jpeg(tile: Image.Image, output: BytesIO) -> None:
    with tile.convert("RGBA") as rgba, Image.new("RGB", tile.size, "white") as background:
        background.paste(rgba, mask=rgba.getchannel("A"))
        background.save(output, format="JPEG", quality=95)


def _parse_fields(
    rows: list[dict],
    width: int,
    height: int,
    offset: tuple[int, int] = (0, 0),
    owned: tuple[float, float, float, float] | None = None,
) -> list[OcrField]:
    fields = []
    width, height = max(width, 1), max(height, 1)
    for field in rows:
        vertices = field.get("boundingPoly", {}).get("vertices", [])
        xs = [v.get("x", 0.0) + offset[0] for v in vertices] or [offset[0]]
        ys = [v.get("y", 0.0) + offset[1] for v in vertices] or [offset[1]]
        x0, x1 = max(0.0, min(xs)), min(width, max(xs))
        y0, y1 = max(0.0, min(ys)), min(height, max(ys))
        if owned and not (owned[0] <= (x0 + x1) / 2 < owned[2] and owned[1] <= (y0 + y1) / 2 < owned[3]):
            continue
        bbox = {
            "x": round(min(1.0, x0 / width), 4),
            "y": round(min(1.0, y0 / height), 4),
            "w": round(max(0.0, x1 - x0) / width, 4),
            "h": round(max(0.0, y1 - y0) / height, 4),
        }
        fields.append(
            OcrField(
                field.get("inferText", ""),
                bbox,
                float(field.get("inferConfidence", 0.0)),
                bool(field.get("lineBreak", False)),
            )
        )
    return fields
