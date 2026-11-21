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
{"lines":[{"image_id":"...","text":"...","bbox":{"x":0,"y":0,"w":0,"h":0},"kind":"dialogue|narration|description|sfx_text|ignore",
"speaker":"인물 이름 또는 null","emotion":"neutral|happy|sad|angry|scared|surprised|confused|whisper|shout",
"ocr_confidence":0.0}],"sfx":[{"image_id":"...","y_ratio":0.0,"sfx_tag":"프리셋 태그"}]}"""


class ClovaStructureClient:
    def __init__(self, settings: Settings, sfx_tags: list[str]) -> None:
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
        user = {
            "known_characters": known_characters,
            "sfx_tags": self.sfx_tags,
            "options": options,
            "pages": [{"image_id": p.image_id, "fields": p.fields} for p in pages],
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
            res = httpx.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            res.raise_for_status()
            return res.json()

        data = with_retries("clova_studio", call, self.max_retries)
        try:
            content = data["result"]["message"]["content"]
            parsed = json.loads(content[content.index("{") : content.rindex("}") + 1])
        except (KeyError, ValueError, TypeError) as exc:
            raise UpstreamError("clova_studio", "invalid response", retryable=False) from exc
        valid_ids = {p.image_id for p in pages}
        result = StructureResult()
        for item in parsed.get("lines", []):
            if item.get("image_id") in valid_ids and item.get("text"):
                result.lines.append(
                    StructuredLine(
                        item["image_id"],
                        item["text"],
                        item.get("bbox"),
                        item.get("kind", "dialogue"),
                        item.get("speaker"),
                        item.get("emotion", "neutral"),
                        item.get("ocr_confidence"),
                    )
                )
        for item in parsed.get("sfx", []):
            if item.get("image_id") in valid_ids and item.get("sfx_tag") in self.sfx_tags:
                result.sfx.append(
                    StructuredSfx(item["image_id"], float(item.get("y_ratio", 0.0)), item["sfx_tag"])
                )
        return result
