"""OCR 단어 조각 → 말풍선 묶음 (좌표 규칙, 외부 호출 없음)."""

from app.services.ocr_grouping import group_fields

W, H = 720, 1000


def _f(text, x, y, w, h, conf=0.95):
    """픽셀 좌표로 조각을 만든다 (저장 형식은 상대 좌표)."""
    return {"text": text, "bbox": {"x": x / W, "y": y / H, "w": w / W, "h": h / H}, "confidence": conf}


def test_one_balloon_split_into_words_becomes_one_line_in_reading_order():
    # 화면 사례: "그런데 상단주님, / 생각보다 … 하시네요." 단어가 따로 오고, '상단주님,'이 살짝 위에 있다.
    fields = [
        _f("상단주님,", 330, 98, 110, 30),
        _f("그런데", 240, 102, 80, 30),
        _f("하시네요.", 430, 142, 110, 30),
        _f("생각보다", 200, 141, 100, 30, conf=0.6),
        _f("대단", 310, 143, 50, 30),
        _f("하", 370, 142, 40, 30),
    ]
    balloons = group_fields(fields, W, H)
    assert [b["text"] for b in balloons] == ["그런데 상단주님, 생각보다 대단 하 하시네요."]
    b = balloons[0]
    assert b["confidence"] == 0.6
    assert b["bbox"]["x"] == round(200 / W, 4) and b["bbox"]["y"] == round(98 / H, 4)
    assert b["bbox"]["w"] == round((540 - 200) / W, 4)


def test_separate_balloons_and_big_sfx_stay_apart():
    fields = [
        _f("여기가", 100, 100, 70, 28),
        _f("어디지?", 180, 100, 80, 28),
        # 오른쪽 아래 다른 말풍선 (세로로 멀다)
        _f("누구야!", 450, 400, 90, 28),
        # 바로 아래 큰 효과음 글씨: 가깝지만 글자 크기가 달라 묶지 않는다
        _f("쾅!", 120, 140, 120, 90),
        # 같은 높이지만 가로로 멀리 떨어진 다른 말풍선
        _f("뭐?", 600, 102, 40, 28),
    ]
    texts = [b["text"] for b in group_fields(fields, W, H)]
    assert texts == ["여기가 어디지?", "뭐?", "쾅!", "누구야!"]


def test_vertical_text_joins_without_spaces():
    fields = [_f(ch, 300, 200 + i * 34, 30, 30) for i, ch in enumerate("누구야")]
    assert [b["text"] for b in group_fields(fields, W, H)] == ["누구야"]


def test_empty_and_unknown_size():
    assert group_fields([], None, None) == []
    out = group_fields(
        [{"text": "  ", "bbox": {}}, {"text": "안녕", "bbox": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.02}}],
        None,
        None,
    )
    assert [b["text"] for b in out] == ["안녕"]
    assert out[0]["confidence"] is None
