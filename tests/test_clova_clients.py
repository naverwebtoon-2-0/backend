"""CLOVA 실제 클라이언트의 요청 형식·응답 해석·오류 매핑 (httpx MockTransport, 실제 키 없음)."""

import base64
import json
from io import BytesIO
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.integrations import sfx_synth
from app.integrations.dubbing import (
    ClovaVoiceClient,
    concat_wavs,
    silent_wav,
    split_sentences,
    voice_form,
    wav_duration_ms,
)
from app.integrations.http import UpstreamError
from app.integrations.ocr import ClovaOcrClient
from app.integrations.storage import FakeStorage
from app.integrations.structure import ClovaStructureClient, PageInput


def _settings(**overrides) -> Settings:
    base = {
        "clova_ocr_invoke_url": "https://ocr.example/general",
        "clova_ocr_secret": "ocr-secret",
        "clova_studio_api_key": "studio-key",
        "clova_voice_client_id": "voice-id",
        "clova_voice_client_secret": "voice-secret",
        "ai_max_retries": 2,
    }
    return Settings(**{**base, **overrides})


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.integrations.http.time.sleep", lambda _: None)


# ── OCR ─────────────────────────────────────────────


def test_ocr_request_and_bbox_normalization():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["secret"] = request.headers["X-OCR-SECRET"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "images": [
                    {
                        "inferResult": "SUCCESS",
                        "fields": [
                            {
                                "inferText": "안녕",
                                "inferConfidence": 0.97,
                                "lineBreak": True,
                                "boundingPoly": {
                                    "vertices": [
                                        {"x": 80, "y": 240},
                                        {"x": 400, "y": 240},
                                        {"x": 400, "y": 300},
                                        {"x": 80, "y": 300},
                                    ]
                                },
                            }
                        ],
                    }
                ]
            },
        )

    client = ClovaOcrClient(_settings(), http=_client(handler))
    fields = client.recognize(b"\x89PNG...", "image/png", 800, 1200, "ko")
    assert seen["secret"] == "ocr-secret"
    assert seen["body"]["version"] == "V2" and seen["body"]["lang"] == "ko"
    assert seen["body"]["images"][0]["format"] == "png"
    assert fields[0].text == "안녕" and fields[0].confidence == 0.97
    assert fields[0].bbox == {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.05}
    assert fields[0].line_break is True


def test_ocr_retries_5xx_then_fails_and_4xx_not_retried():
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(UpstreamError):
        ClovaOcrClient(_settings(), http=_client(flaky)).recognize(b"x", "image/png", 1, 1, "ko")
    assert len(calls) == 3  # 최초 1 + 재시도 2

    calls.clear()

    def bad(request):
        calls.append(1)
        return httpx.Response(400)

    with pytest.raises(UpstreamError) as exc:
        ClovaOcrClient(_settings(), http=_client(bad)).recognize(b"x", "image/png", 1, 1, "ko")
    assert len(calls) == 1 and exc.value.retryable is False


def test_ocr_not_configured():
    with pytest.raises(UpstreamError):
        ClovaOcrClient(_settings(clova_ocr_secret=""), http=_client(lambda r: httpx.Response(200))).recognize(
            b"x", "image/png", 1, 1, "ko"
        )


def test_ocr_uses_domain_language_and_converts_webp():
    source = BytesIO()
    Image.new("RGBA", (80, 60), (0, 0, 0, 0)).save(source, format="WEBP")

    def handler(request):
        body = json.loads(request.content)
        assert "lang" not in body
        uploaded = body["images"][0]
        assert uploaded["format"] == "jpg"
        with Image.open(BytesIO(base64.b64decode(uploaded["data"]))) as image:
            assert image.size == (80, 60)
            assert image.getpixel((0, 0)) == (255, 255, 255)
        return httpx.Response(200, json={"images": [{"inferResult": "SUCCESS", "fields": []}]})

    client = ClovaOcrClient(_settings(), http=_client(handler))
    assert client.recognize(source.getvalue(), "image/webp", 80, 60, "en") == []


def test_ocr_accepts_pages_without_text():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "images": [
                    {
                        "inferResult": "FAILURE",
                        "message": '400 Bad Request: "{"errorCode":"ENGN-001","errorMsg":"NO_TEXT"}"',
                    }
                ]
            },
        )

    client = ClovaOcrClient(_settings(), http=_client(handler))
    assert client.recognize(b"image", "image/png", 80, 60, "ko") == []


def test_ocr_long_page_tiles_keep_coordinates_and_ignore_duplicate_text():
    source = BytesIO()
    Image.new("RGB", (40, 10277), "white").save(source, format="PNG")
    sizes = []

    def row(text, y):
        return {"inferText": text, "boundingPoly": {"vertices": [{"x": 1, "y": y}, {"x": 5, "y": y + 4}]}}

    def handler(request):
        image = json.loads(request.content)["images"][0]
        with Image.open(BytesIO(base64.b64decode(image["data"]))) as tile:
            sizes.append(tile.size)
        fields = [row("겹친 글자", 7850)] if len(sizes) == 1 else [row("겹친 글자", 107), row("아래", 700)]
        return httpx.Response(200, json={"images": [{"inferResult": "SUCCESS", "fields": fields}]})

    fields = ClovaOcrClient(_settings(), http=_client(handler)).recognize(
        source.getvalue(), "image/png", 40, 10277, "ko"
    )
    assert sizes == [(40, 7999), (40, 2534)]
    assert [field.text for field in fields] == ["겹친 글자", "아래"]
    assert fields[1].bbox["y"] == round(8443 / 10277, 4)


def test_ocr_http_200_reports_engine_failure():
    def handler(request):
        message = '400 Bad Request: "{"errorCode":"ENGN-010","errorMsg":"Image resolution limit exceeded"}"'
        return httpx.Response(
            200, json={"requestId": "ocr-123", "images": [{"inferResult": "ERROR", "message": message}]}
        )

    with pytest.raises(UpstreamError) as caught:
        ClovaOcrClient(_settings(), http=_client(handler)).recognize(b"image", "image/png", 80, 60, "ko")
    detail = caught.value.details()["upstream"]
    assert detail["status"] == 200 and detail["code"] == "ENGN-010"
    assert detail["message"] == "Image resolution limit exceeded" and detail["request_id"] == "ocr-123"


def test_provider_http_error_returns_code_and_masks_private_url():
    def handler(request):
        return httpx.Response(
            401,
            headers={"X-NCP-APIGW-Request-Id": "gw-123"},
            json={
                "error": {
                    "errorCode": "200",
                    "message": "Authentication failed at https://private.example/path",
                }
            },
        )

    with pytest.raises(UpstreamError) as caught:
        ClovaVoiceClient(_settings(), http=_client(handler)).synthesize("안녕", "nara", {}, "neutral")
    detail = caught.value.details()["upstream"]
    assert detail["status"] == 401 and detail["code"] == "200" and not detail["retryable"]
    assert detail["request_id"] == "gw-123" and detail["message"] == "Authentication failed at [가림]"


# ── Studio ──────────────────────────────────────────


def _completion(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": {"code": "20000"},
            "result": {"message": {"role": "assistant", "content": "결과:\n" + json.dumps(content)}},
        },
    )


def _balloon(text, y, conf=0.9):
    return {"text": text, "bbox": {"x": 0.1, "y": y, "w": 0.3, "h": 0.04}, "confidence": conf}


def test_structure_answers_by_id_and_keeps_original_text():
    pages = [
        PageInput("img1", [_balloon("여기가", 0.2), _balloon("어디지?", 0.25, 0.7), _balloon("삐걱", 0.5)]),
        PageInput("img2", [_balloon("워터마크", 0.9)]),
    ]
    content = {
        "lines": [
            # 잘못 나뉜 말풍선 합치기. 모델이 text를 고쳐 써도 무시하고 원문을 쓴다.
            {
                "ids": ["b2", "b1"],
                "text": "고친 문장",
                "kind": "dialogue",
                "speaker": "민준",
                "emotion": "confused",
            },
            {"ids": ["b3"], "kind": "sfx_text", "speaker": None, "emotion": "neutral"},
            {"ids": ["b1", "없는id"], "kind": "dialogue"},  # 이미 쓴 id·없는 id는 무시
        ],
        "sfx": [{"id": "b3", "sfx_tag": "door_open"}, {"id": "b3", "sfx_tag": "unknown"}],
    }

    def handler(request):
        assert request.headers["Authorization"] == "Bearer studio-key"
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        user = json.loads(body["messages"][1]["content"])
        assert [b["id"] for p in user["pages"] for b in p["balloons"]] == ["b1", "b2", "b3", "b4"]
        return _completion(content)

    client = ClovaStructureClient(_settings(), ["door_open"], http=_client(handler))
    result = client.structure(pages, ["민준"], {"sfx": True})
    assert [(ln.image_id, ln.text, ln.speaker, ln.kind) for ln in result.lines] == [
        ("img1", "여기가 어디지?", "민준", "dialogue"),
        ("img1", "삐걱", None, "sfx_text"),
        # 모델이 빠뜨린 말풍선은 화자 미지정 대사로 남는다
        ("img2", "워터마크", None, "dialogue"),
    ]
    merged = result.lines[0]
    assert merged.ocr_confidence == 0.7
    assert merged.bbox == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.09}
    assert [(s.image_id, s.sfx_tag, s.y_ratio) for s in result.sfx] == [("img1", "door_open", 0.5)]


def test_structure_chunks_requests_and_passes_context():
    pages = [PageInput(f"img{i}", [_balloon(f"대사{i}", 0.1)]) for i in range(5)]
    requests = []

    def handler(request):
        user = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(user)
        ids = [b["id"] for p in user["pages"] for b in p["balloons"]]
        return _completion({"lines": [{"ids": [i], "kind": "dialogue", "speaker": "서연"} for i in ids]})

    client = ClovaStructureClient(_settings(), [], http=_client(handler))
    client.chunk_balloons = 2
    result = client.structure(pages, [], {})
    assert len(requests) == 3
    assert requests[0]["known_characters"] == [] and requests[0]["previous_lines"] == []
    assert requests[1]["known_characters"] == ["서연"]
    assert requests[1]["previous_lines"][-1] == {"speaker": "서연", "text": "대사1"}
    assert [ln.text for ln in result.lines] == [f"대사{i}" for i in range(5)]


def test_structure_skips_call_when_no_balloons():
    def handler(request):
        raise AssertionError("호출하면 안 된다")

    result = ClovaStructureClient(_settings(), [], http=_client(handler)).structure(
        [PageInput("a", [])], [], {}
    )
    assert result.lines == [] and result.sfx == []


def test_structure_invalid_response():
    def handler(request):
        return httpx.Response(
            200, json={"status": {"code": "20000"}, "result": {"message": {"content": "no json"}}}
        )

    pages = [PageInput("a", [_balloon("안녕", 0.1)])]
    with pytest.raises(UpstreamError) as caught:
        ClovaStructureClient(_settings(), [], http=_client(handler)).structure(pages, [], {})
    # 파이프라인이 말풍선 원문으로 대체하는 조건
    assert caught.value.code == "INVALID_RESPONSE" and caught.value.status == 200

    def failed(request):
        return httpx.Response(200, json={"status": {"code": "40001"}, "result": None})

    with pytest.raises(UpstreamError):
        ClovaStructureClient(_settings(), [], http=_client(failed)).structure(pages, [], {})


def test_structure_preserves_ocr_for_missing_page():
    def handler(request):
        content = json.dumps({"lines": [{"image_id": "invalid", "text": "다른 페이지"}]})
        return httpx.Response(200, json={"result": {"message": {"content": content}}})

    fields = [{"text": "OCR 원문", "bbox": {"y": 0.4}, "confidence": 0.9}]
    result = ClovaStructureClient(_settings(), [], http=_client(handler)).structure(
        [PageInput("stored-id", fields), PageInput("blank", [])], [], {}
    )
    assert [(line.image_id, line.text, line.ocr_confidence) for line in result.lines] == [
        ("stored-id", "OCR 원문", 0.9)
    ]


# ── Voice ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("speaker", "params", "emotion", "expected"),
    [
        ("vdain", {}, "neutral", {}),
        ("vdain", {"speed": 1, "pitch": -1}, "sad", {"speed": "1", "pitch": "-1", "emotion": "1"}),
        ("vdain", {"emotion_strength": 2}, "happy", {"emotion": "2", "emotion-strength": "2"}),
        ("nara", {"emotion_strength": 2}, "angry", {}),  # nara: 분노·강도 미지원
        ("nara", {}, "sad", {"emotion": "1"}),
        ("jinho", {}, "happy", {}),  # 감정 미지원 화자
        ("vara", {}, "shout", {"volume": "3", "emotion": "3", "emotion-strength": "2"}),
        ("vara", {"volume": 2}, "whisper", {"volume": "-3"}),
        ("vara", {"speed": 99, "pitch": -99, "alpha": "x"}, "confused", {"speed": "10", "pitch": "-5"}),
    ],
)
def test_voice_form_mapping(speaker, params, emotion, expected):
    form = voice_form(speaker, params, emotion)
    assert form == {"speaker": speaker, "format": "wav", "sampling-rate": "24000", **expected}


def test_split_sentences_respects_200_chars():
    assert split_sentences("안녕. 반가워!") == ["안녕.", "반가워!"]
    long = "가" * 450
    chunks = split_sentences(long)
    assert [len(c) for c in chunks] == [200, 200, 50]
    words = " ".join(["단어"] * 120)
    assert all(len(c) <= 200 for c in split_sentences(words))
    assert split_sentences("   ") == []


def test_voice_synthesize_concatenates_chunks():
    forms = []

    def handler(request):
        assert request.headers["X-NCP-APIGW-API-KEY-ID"] == "voice-id"
        assert request.headers["X-NCP-APIGW-API-KEY"] == "voice-secret"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, content=silent_wav(500, 24000), headers={"content-type": "audio/wav"})

    client = ClovaVoiceClient(_settings(), http=_client(handler))
    result = client.synthesize("첫 문장입니다. 두 번째 문장!", "vdain", {"speed": 0}, "happy")
    assert [f["text"] for f in forms] == ["첫 문장입니다.", "두 번째 문장!"]
    assert forms[0]["speaker"] == "vdain" and forms[0]["emotion"] == "2"
    assert result.content_type == "audio/wav" and result.duration_ms == 1000
    assert wav_duration_ms(result.data) == 1000


def test_voice_errors():
    client = ClovaVoiceClient(
        _settings(), http=_client(lambda r: httpx.Response(400, json={"error": "VS02"}))
    )
    with pytest.raises(UpstreamError) as exc:
        client.synthesize("안녕", "nope", {}, "neutral")
    assert exc.value.retryable is False
    with pytest.raises(UpstreamError):
        client.synthesize("가" * 2001, "vdain", {}, "neutral")
    unconfigured = ClovaVoiceClient(
        _settings(clova_voice_client_id=""), http=_client(lambda r: httpx.Response(200))
    )
    with pytest.raises(UpstreamError):
        unconfigured.synthesize("안녕", "vdain", {}, "neutral")


def test_concat_wavs_rejects_mixed_formats():
    with pytest.raises(UpstreamError):
        concat_wavs([silent_wav(100, 8000), silent_wav(100, 24000)])


def test_sfx_preset_from_storage_then_builtin(tmp_path):
    storage = FakeStorage(tmp_path, "http://x", "secret-secret-secret-secret-1234")
    client = ClovaVoiceClient(_settings(), storage=storage, http=_client(lambda r: httpx.Response(500)))
    builtin = client.synthesize_sfx("door_open")
    assert builtin.duration_ms == 900 and wav_duration_ms(builtin.data) == 900
    storage.put("sfx/presets/door_open.wav", silent_wav(1234, 8000), "audio/wav")
    preset = client.synthesize_sfx("door_open")
    assert preset.duration_ms == 1234
    with pytest.raises(UpstreamError):
        client.synthesize_sfx("unknown")


@pytest.mark.parametrize("tag", sorted(sfx_synth.GENERATORS))
def test_builtin_sfx_generator(tag):
    data, duration = sfx_synth.generate(tag)
    assert wav_duration_ms(data) == duration > 0
    assert sfx_synth.generate(tag)[0] == data  # 결정적
