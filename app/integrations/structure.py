"""CLOVA Studio(HyperCLOVA) 추상화: OCR 결과 → 대사·화자·감정·종류·효과음 태그."""

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import Settings
from app.integrations.http import UpstreamError, with_retries

logger = logging.getLogger(__name__)


@dataclass
class PageInput:
    image_id: str
    fields: list[dict]  # {"text", "bbox", "confidence"}


@dataclass
class StructuredLine:
    image_id: str
    text: str
    bbox: dict[str, float] | None
    kind: str
    speaker: str | None
    emotion: str
    ocr_confidence: float | None


@dataclass
class StructuredSfx:
    image_id: str
    y_ratio: float
    sfx_tag: str


@dataclass
class StructureResult:
    lines: list[StructuredLine] = field(default_factory=list)
    sfx: list[StructuredSfx] = field(default_factory=list)


class StructureClient(Protocol):
    def structure(
        self, pages: list[PageInput], known_characters: list[str], options: dict
    ) -> StructureResult: ...


_FAKE_SFX = {"쾅!": "thunder_near", "삐걱": "door_open", "쏴아": "rain_loop"}


class FakeStructureClient:
    def structure(
        self, pages: list[PageInput], known_characters: list[str], options: dict
    ) -> StructureResult:
        result = StructureResult()
        for page in pages:
            for i, f in enumerate(page.fields):
                text = f["text"].strip()
                bbox = f.get("bbox")
                conf = f.get("confidence")
                if text in _FAKE_SFX:
                    result.lines.append(
                        StructuredLine(page.image_id, text, bbox, "sfx_text", None, "neutral", conf)
                    )
                    if options.get("sfx", True):
                        y = bbox["y"] if bbox else 0.0
                        result.sfx.append(StructuredSfx(page.image_id, y, _FAKE_SFX[text]))
                    continue
                if i == 0:
                    kind = "narration" if options.get("narration", True) else "ignore"
                    result.lines.append(
                        StructuredLine(page.image_id, text, bbox, kind, None, "neutral", conf)
                    )
                    continue
                speaker = "민준" if text.endswith("?") else None
                emotion = "confused" if text.endswith("?") else "surprised"
                result.lines.append(
                    StructuredLine(page.image_id, text, bbox, "dialogue", speaker, emotion, conf)
                )
        return result


_SYSTEM_PROMPT = """너는 웹툰 원고의 OCR 결과를 정리하는 도우미다.
입력은 이미지별 텍스트 조각(text, bbox, confidence) 목록이다. 말풍선 단위로 묶고 각 항목을 분류해
아래 JSON만 출력한다.
각 항목의 image_id는 해당 입력 페이지의 image_id를 그대로 복사한다.
말풍선이 아닌 텍스트도 누락하지 말고 필요하면 ignore로 분류한다.
{"lines":[{"image_id":"...","text":"...","bbox":{"x":0,"y":0,"w":0,"h":0},"kind":"dialogue|narration|description|sfx_text|ignore",
"speaker":"인물 이름 또는 null","emotion":"neutral|happy|sad|angry|scared|surprised|confused|whisper|shout",
"ocr_confidence":0.0}],"sfx":[{"image_id":"...","y_ratio":0.0,"sfx_tag":"프리셋 태그"}]}"""


class ClovaStructureClient:
    def __init__(self, settings: Settings, sfx_tags: list[str], http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=settings.ai_timeout_seconds)
        self.url = settings.clova_studio_url
        self.api_key = settings.clova_studio_api_key
        self.timeout = settings.ai_timeout_seconds
        self.max_retries = settings.ai_max_retries
        self.sfx_tags = sfx_tags

    def structure(
        self, pages: list[PageInput], known_characters: list[str], options: dict
    ) -> StructureResult:
        if not self.api_key:
            raise UpstreamError("clova_studio", "not configured", retryable=False)
        # 모델이 긴 이미지 ID를 바꾸지 않도록 짧은 번호로 전달한다.
        pages_by_id = {f"p{index}": page for index, page in enumerate(pages, start=1)}
        user = {
            "known_characters": known_characters,
            "sfx_tags": self.sfx_tags,
            "options": options,
            "pages": [{"image_id": ref, "fields": page.fields} for ref, page in pages_by_id.items()],
        }
        body = {
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "maxTokens": 4096,
        }

        def call() -> dict:
            res = self.http.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            res.raise_for_status()
            return res.json()

        data = with_retries("clova_studio", call, self.max_retries)
        status = (data.get("status") or {}).get("code")
        if status not in (None, "20000"):
            raise UpstreamError("clova_studio", f"status {status}", retryable=False)
        try:
            content = data["result"]["message"]["content"]
            parsed = json.loads(content[content.index("{") : content.rindex("}") + 1])
        except (KeyError, ValueError, TypeError) as exc:
            raise UpstreamError("clova_studio", "invalid response", retryable=False) from exc
        result = StructureResult()
        for item in parsed.get("lines", []):
            ref = item.get("image_id")
            page = pages_by_id.get(ref) if isinstance(ref, str) else None
            if page is not None and item.get("text"):
                result.lines.append(
                    StructuredLine(
                        page.image_id,
                        item["text"],
                        item.get("bbox"),
                        item.get("kind", "dialogue"),
                        item.get("speaker"),
                        item.get("emotion", "neutral"),
                        item.get("ocr_confidence"),
                    )
                )
        for item in parsed.get("sfx", []):
            ref = item.get("image_id")
            page = pages_by_id.get(ref) if isinstance(ref, str) else None
            if page is not None and item.get("sfx_tag") in self.sfx_tags:
                result.sfx.append(
                    StructuredSfx(page.image_id, float(item.get("y_ratio", 0.0)), item["sfx_tag"])
                )
        # 모델 응답에서 빠진 페이지는 OCR 원문을 검수할 수 있도록 남긴다.
        structured_pages = {line.image_id for line in result.lines}
        for page in pages:
            if page.image_id in structured_pages:
                continue
            for ocr_field in page.fields:
                if text := ocr_field["text"].strip():
                    result.lines.append(
                        StructuredLine(
                            page.image_id,
                            text,
                            ocr_field.get("bbox"),
                            "dialogue",
                            None,
                            "neutral",
                            ocr_field.get("confidence"),
                        )
                    )
        return result
