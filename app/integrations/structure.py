"""CLOVA Studio(HyperCLOVA) 추상화: 말풍선 목록 → 대사·화자·감정·종류·효과음 태그.

입력은 OCR 조각을 좌표로 미리 묶은 말풍선이다(app.services.ocr_grouping). 모델은 id로만 답하고,
글자와 좌표는 서버가 원본 말풍선에서 다시 만든다. 그래서 모델이 원문을 고쳐 쓰거나 좌표를 틀리게 써도
대사 문구와 위치는 바뀌지 않는다.
"""

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
    balloons: list[dict]  # 좌표로 묶은 말풍선 {"text", "bbox", "confidence"} (위→아래 순서)


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
            for i, f in enumerate(page.balloons):
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


_SYSTEM_PROMPT = """너는 웹툰 원고의 말풍선을 분류하는 도우미다.
입력 pages의 balloons는 OCR 결과를 좌표로 이미 말풍선 단위로 묶은 것이다(id, text, bbox, confidence).
text를 고쳐 쓰거나 요약하지 말고, id로만 답한다. 아래 JSON만 출력한다.
{"lines":[{"ids":["b1"],"kind":"dialogue|narration|description|sfx_text|ignore",
"speaker":"인물 이름 또는 null","emotion":"neutral|happy|sad|angry|scared|surprised|confused|whisper|shout"}],
"sfx":[{"id":"b3","sfx_tag":"sfx_tags 중 하나"}]}
규칙:
- 모든 id를 lines에 정확히 한 번씩 넣는다. 읽는 순서대로 쓴다.
- 한 말풍선이 잘못 둘로 나뉜 경우에만 같은 이미지의 연속된 id를 ids 하나에 함께 넣는다.
- speaker는 known_characters와 previous_lines를 참고하고, 모르면 null로 둔다.
- sfx는 효과음 글자(sfx_text)에 어울리는 sfx_tags가 있을 때만 넣는다."""

# 한 번의 Studio 요청에 넣는 말풍선 수 (응답이 maxTokens에서 잘리지 않도록 나눠 보낸다)
STRUCTURE_CHUNK_BALLOONS = 60
# 다음 요청에 문맥으로 넘기는 직전 대사 수
STRUCTURE_CONTEXT_LINES = 5


def _chunks(pages: list[PageInput], limit: int) -> list[list[PageInput]]:
    chunks: list[list[PageInput]] = []
    current: list[PageInput] = []
    size = 0
    for page in pages:
        n = len(page.balloons)
        if current and size + n > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(page)
        size += n
    if current:
        chunks.append(current)
    return chunks


def _union_bbox(boxes: list[dict | None]) -> dict[str, float] | None:
    valid = [b for b in boxes if b]
    if not valid:
        return None
    x0 = min(b["x"] for b in valid)
    y0 = min(b["y"] for b in valid)
    x1 = max(b["x"] + b["w"] for b in valid)
    y1 = max(b["y"] + b["h"] for b in valid)
    return {"x": round(x0, 4), "y": round(y0, 4), "w": round(x1 - x0, 4), "h": round(y1 - y0, 4)}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


class ClovaStructureClient:
    def __init__(self, settings: Settings, sfx_tags: list[str], http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=settings.ai_timeout_seconds)
        self.url = settings.clova_studio_url
        self.api_key = settings.clova_studio_api_key
        self.timeout = settings.ai_timeout_seconds
        self.max_retries = settings.ai_max_retries
        self.sfx_tags = sfx_tags
        self.chunk_balloons = STRUCTURE_CHUNK_BALLOONS

    def _complete(self, user: dict) -> dict:
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
        if not isinstance(parsed, dict):
            raise UpstreamError("clova_studio", "invalid response", retryable=False)
        return parsed

    def structure(
        self, pages: list[PageInput], known_characters: list[str], options: dict
    ) -> StructureResult:
        if not self.api_key:
            raise UpstreamError("clova_studio", "not configured", retryable=False)
        result = StructureResult()
        speakers = list(known_characters)
        previous: list[dict] = []
        counter = 0
        for chunk in _chunks(pages, self.chunk_balloons):
            # id → (image_id, 말풍선, 페이지 안 순서)
            index: dict[str, tuple[str, dict, int]] = {}
            payload_pages = []
            for page in chunk:
                items = []
                for pos, balloon in enumerate(page.balloons):
                    counter += 1
                    bid = f"b{counter}"
                    index[bid] = (page.image_id, balloon, pos)
                    items.append(
                        {
                            "id": bid,
                            "text": balloon["text"],
                            "bbox": balloon.get("bbox"),
                            "confidence": balloon.get("confidence"),
                        }
                    )
                payload_pages.append({"image_id": page.image_id, "balloons": items})
            if not index:
                continue
            parsed = self._complete(
                {
                    "known_characters": speakers,
                    "previous_lines": previous[-STRUCTURE_CONTEXT_LINES:],
                    "sfx_tags": self.sfx_tags,
                    "options": options,
                    "pages": payload_pages,
                }
            )
            page_order = {page.image_id: i for i, page in enumerate(chunk)}
            used: set[str] = set()
            lines: list[tuple[int, int, StructuredLine]] = []
            for item in _as_list(parsed.get("lines")):
                if not isinstance(item, dict):
                    continue
                raw_ids = item["ids"] if isinstance(item.get("ids"), list) else [item.get("id")]
                ids = [i for i in raw_ids if isinstance(i, str) and i in index and i not in used]
                if not ids:
                    continue
                image_id = index[ids[0]][0]
                ids = sorted((i for i in ids if index[i][0] == image_id), key=lambda i: index[i][2])
                used.update(ids)
                balloons = [index[i][1] for i in ids]
                confs = [b["confidence"] for b in balloons if b.get("confidence") is not None]
                speaker = item.get("speaker")
                line = StructuredLine(
                    image_id,
                    " ".join(b["text"] for b in balloons),
                    _union_bbox([b.get("bbox") for b in balloons]),
                    str(item.get("kind") or "dialogue"),
                    speaker.strip() or None if isinstance(speaker, str) else None,
                    str(item.get("emotion") or "neutral"),
                    min(confs) if confs else None,
                )
                lines.append((page_order[image_id], index[ids[0]][2], line))
            # 모델이 빠뜨린 말풍선은 버리지 않고 화자 미지정 대사로 남긴다(작가가 검수에서 정리).
            for bid, (image_id, balloon, pos) in index.items():
                if bid not in used:
                    lines.append(
                        (
                            page_order[image_id],
                            pos,
                            StructuredLine(
                                image_id,
                                balloon["text"],
                                balloon.get("bbox"),
                                "dialogue",
                                None,
                                "neutral",
                                balloon.get("confidence"),
                            ),
                        )
                    )
            lines.sort(key=lambda t: (t[0], t[1]))
            for _, _, line in lines:
                result.lines.append(line)
                if line.speaker and line.speaker not in speakers:
                    speakers.append(line.speaker)
                if line.kind in ("dialogue", "narration"):
                    previous.append({"speaker": line.speaker, "text": line.text})
            for item in _as_list(parsed.get("sfx")):
                if not isinstance(item, dict):
                    continue
                bid = item.get("id")
                if isinstance(bid, str) and bid in index and item.get("sfx_tag") in self.sfx_tags:
                    image_id, balloon, _ = index[bid]
                    y = float((balloon.get("bbox") or {}).get("y", 0.0))
                    result.sfx.append(StructuredSfx(image_id, y, item["sfx_tag"]))
        return result
