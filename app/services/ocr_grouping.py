"""OCR 단어 조각 → 말풍선 단위 묶음 (외부 호출 없이 좌표만 사용).

CLOVA OCR General은 단어마다 field를 하나씩 준다. 그대로 쓰면 말풍선 하나가 대사 여러 줄로 쪼개지고
순서도 어긋나므로, 구조화(CLOVA Studio) 전에 다음 순서로 묶는다.

1. 줄: 세로로 충분히 겹치고 가로로 가까운 조각을 한 줄로 잇는다 (줄 안은 왼쪽→오른쪽).
2. 말풍선: 위아래로 가깝고 가운데가 맞거나 가로로 겹치며 글자 크기가 비슷한 줄을 묶는다.

bbox는 이미지 대비 상대 좌표(0~1)라 가로·세로 단위가 다르므로, 이미지 크기를 곱해 픽셀 단위로 비교한다.
원문 글자는 바꾸지 않고 이어 붙이기만 한다.
"""

from dataclasses import dataclass, field

# 같은 줄: 세로 겹침이 작은 쪽 높이의 이 비율 이상
LINE_VERTICAL_OVERLAP = 0.5
# 같은 줄: 조각 사이 가로 간격이 글자 높이의 이 배수 이하
LINE_HORIZONTAL_GAP = 1.5
# 같은 말풍선: 줄 사이 세로 간격이 글자 높이의 이 배수 이하
BALLOON_VERTICAL_GAP = 1.0
# 같은 말풍선: 두 줄 글자 높이 비율이 이 값 이상 (효과음 큰 글씨와 대사를 섞지 않는다)
BALLOON_HEIGHT_RATIO = 0.6
# 같은 말풍선: 가운데 x 차이가 넓은 쪽 줄 너비의 이 비율 이하 이거나
BALLOON_CENTER_SHIFT = 0.5
# 가로 겹침이 좁은 쪽 줄 너비의 이 비율 이상
BALLOON_HORIZONTAL_OVERLAP = 0.3


@dataclass
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    def union(self, other: "_Box") -> "_Box":
        return _Box(
            min(self.x0, other.x0), min(self.y0, other.y0), max(self.x1, other.x1), max(self.y1, other.y1)
        )


@dataclass
class _Piece:
    text: str
    box: _Box
    confidence: float | None


@dataclass
class _Line:
    pieces: list[_Piece] = field(default_factory=list)
    box: _Box | None = None

    def add(self, piece: _Piece) -> None:
        self.pieces.append(piece)
        self.box = piece.box if self.box is None else self.box.union(piece.box)

    @property
    def text(self) -> str:
        return " ".join(p.text for p in sorted(self.pieces, key=lambda p: p.box.x0))

    @property
    def char_height(self) -> float:
        return sum(p.box.h for p in self.pieces) / len(self.pieces)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _to_piece(item: dict, width: float, height: float) -> _Piece | None:
    text = str(item.get("text") or "").strip()
    bbox = item.get("bbox") or {}
    if not text:
        return None
    x = float(bbox.get("x", 0.0)) * width
    y = float(bbox.get("y", 0.0)) * height
    w = max(float(bbox.get("w", 0.0)) * width, 1e-6)
    h = max(float(bbox.get("h", 0.0)) * height, 1e-6)
    conf = item.get("confidence")
    return _Piece(text, _Box(x, y, x + w, y + h), None if conf is None else float(conf))


def _same_line(line: _Line, box: _Box, char_h: float) -> bool:
    assert line.box is not None
    ref_h = min(char_h, line.char_height)
    if _overlap(line.box.y0, line.box.y1, box.y0, box.y1) < LINE_VERTICAL_OVERLAP * ref_h:
        return False
    gap = max(box.x0 - line.box.x1, line.box.x0 - box.x1, 0.0)
    return gap <= LINE_HORIZONTAL_GAP * max(char_h, line.char_height)


def _build_lines(pieces: list[_Piece]) -> list[_Line]:
    # 가로쓰기 줄은 오른쪽으로 자라므로 왼쪽 조각부터 붙인다.
    lines: list[_Line] = []
    for piece in sorted(pieces, key=lambda p: (p.box.x0, p.box.y0)):
        target = next((line for line in lines if _same_line(line, piece.box, piece.box.h)), None)
        if target is None:
            target = _Line()
            lines.append(target)
        target.add(piece)
    # 붙이는 순서 때문에 따로 시작한 같은 줄을 합친다.
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(lines):
            for b in lines[i + 1 :]:
                assert b.box is not None
                if _same_line(a, b.box, b.char_height):
                    for piece in b.pieces:
                        a.add(piece)
                    lines.remove(b)
                    merged = True
                    break
            if merged:
                break
    return lines


def _same_balloon(last: _Line, line: _Line) -> bool:
    assert last.box is not None and line.box is not None
    h_last, h_line = last.char_height, line.char_height
    if min(h_last, h_line) / max(h_last, h_line) < BALLOON_HEIGHT_RATIO:
        return False
    gap = line.box.y0 - last.box.y1
    if gap > BALLOON_VERTICAL_GAP * max(h_last, h_line):
        return False
    centered = abs(line.box.cx - last.box.cx) <= BALLOON_CENTER_SHIFT * max(line.box.w, last.box.w)
    overlap = _overlap(last.box.x0, last.box.x1, line.box.x0, line.box.x1)
    return centered or overlap >= BALLOON_HORIZONTAL_OVERLAP * min(line.box.w, last.box.w)


def _join_lines(lines: list[_Line]) -> str:
    # 세로쓰기(한 줄에 한 글자씩)는 띄어쓰기 없이 잇는다.
    if len(lines) > 1 and all(len(line.pieces) == 1 and len(line.text) == 1 for line in lines):
        return "".join(line.text for line in lines)
    return " ".join(line.text for line in lines)


def group_fields(fields: list[dict], width: int | None, height: int | None) -> list[dict]:
    """OCR 조각 목록을 말풍선 목록으로 묶는다. 결과는 위→아래, 왼쪽→오른쪽 순서.

    반환 항목: {"text", "bbox"(상대 좌표), "confidence"(가장 낮은 값)}
    """
    w = float(width or 1)
    h = float(height or 1)
    pieces = [p for p in (_to_piece(item, w, h) for item in fields) if p is not None]
    balloons: list[list[_Line]] = []
    for line in sorted(_build_lines(pieces), key=lambda ln: (ln.box.y0, ln.box.x0)):  # type: ignore[union-attr]
        candidates = [b for b in balloons if _same_balloon(b[-1], line)]
        if candidates:
            # 바로 위 줄과 가장 가까운 말풍선에 붙인다.
            best = min(candidates, key=lambda b: line.box.y0 - b[-1].box.y1)  # type: ignore[union-attr]
            best.append(line)
        else:
            balloons.append([line])

    result: list[dict] = []
    for lines in balloons:
        box = lines[0].box
        assert box is not None
        for line in lines[1:]:
            box = box.union(line.box)  # type: ignore[arg-type]
        confs = [p.confidence for line in lines for p in line.pieces if p.confidence is not None]
        result.append(
            {
                "text": _join_lines(lines),
                "bbox": {
                    "x": round(box.x0 / w, 4),
                    "y": round(box.y0 / h, 4),
                    "w": round(box.w / w, 4),
                    "h": round(box.h / h, 4),
                },
                "confidence": min(confs) if confs else None,
            }
        )
    result.sort(key=lambda b: (b["bbox"]["y"], b["bbox"]["x"]))
    return result
