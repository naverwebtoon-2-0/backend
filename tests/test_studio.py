import pytest

from app.models import DialogueLine
from app.services.episodes import line_flags
from tests.conftest import create_episode, create_series, signup, upload_images
from tests.helpers import processed_episode


def _studio(client, h, episode_id, **params):
    res = client.get(f"/v1/episodes/{episode_id}/studio", params=params, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def _line(studio, kind="dialogue", text=None):
    return next(
        line for line in studio["lines"] if line["kind"] == kind and (text is None or line["text"] == text)
    )


def test_studio_bundle(client, integrations):
    h, _, e, image_ids = processed_episode(client, integrations, images=2)
    studio = _studio(client, h, e["id"])
    assert studio["episode"]["state"] == "needs_review"
    assert [img["id"] for img in studio["images"]] == image_ids
    assert {c["kind"] for c in studio["characters"]} == {"narration", "character"}
    line = _line(studio, text="여기가 어디지?")
    assert line["audio"]["status"] == "ready" and line["audio"]["url"] and line["audio"]["duration_ms"] > 0
    assert line["bbox"]["y"] == pytest.approx(0.231) and line["y_ratio"] == pytest.approx(0.231)
    assert _line(studio, kind="sfx_text")["audio"] is None
    assert studio["sfx_cues"][0]["sfx_tag"] == "thunder_near" and studio["sfx_cues"][0]["volume"] == 1.0

    filtered = _studio(client, h, e["id"], image_id=image_ids[1])
    assert {line["image_id"] for line in filtered["lines"]} == {image_ids[1]}
    assert {c["image_id"] for c in filtered["sfx_cues"]} == {image_ids[1]}
    assert client.get(f"/v1/episodes/{e['id']}/studio?image_id=nope", headers=h).status_code == 404

    lines = client.get(f"/v1/episodes/{e['id']}/lines?image_id={image_ids[0]}", headers=h).json()["items"]
    assert [line["y_ratio"] for line in lines] == sorted(line["y_ratio"] for line in lines)


@pytest.mark.parametrize(
    ("patch", "expected_status"),
    [
        ({"text": "여기가 어디야?"}, "stale"),
        ({"emotion": "scared"}, "stale"),
        ({"kind": "narration"}, "stale"),
        ({"kind": "ignore"}, None),
        ({"bbox": {"x": 0.1, "y": 0.3, "w": 0.2, "h": 0.05}}, "ready"),
        ({"y_ratio": 0.5}, "ready"),
        ({"text": "여기가 어디지?"}, "ready"),  # 같은 값이면 stale 아님
    ],
)
def test_line_patch_stale_rules(client, integrations, patch, expected_status):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _line(_studio(client, h, e["id"]), text="여기가 어디지?")
    res = client.patch(f"/v1/lines/{line['id']}", json=patch, headers=h)
    assert res.status_code == 200, res.text
    body = res.json()
    if expected_status is None:
        assert body["audio"] is None
    else:
        assert body["audio"]["status"] == expected_status
    if expected_status == "stale":
        assert "audio_stale" in body["flags"]


def test_line_patch_follows_bbox_and_speaker(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    studio = _studio(client, h, e["id"])
    line = _line(studio, text="여기가 어디지?")
    res = client.patch(
        f"/v1/lines/{line['id']}", json={"bbox": {"x": 0.1, "y": 0.4, "w": 0.2, "h": 0.05}}, headers=h
    )
    assert res.json()["y_ratio"] == pytest.approx(0.4)
    unknown = _line(studio, text="누구야!")
    assert "speaker_unknown" in unknown["flags"]
    char = next(c for c in studio["characters"] if c["kind"] == "character")
    res = client.patch(f"/v1/lines/{unknown['id']}", json={"character_id": char["id"]}, headers=h)
    assert "speaker_unknown" not in res.json()["flags"] and res.json()["audio"]["status"] == "stale"
    assert (
        client.patch(f"/v1/lines/{unknown['id']}", json={"character_id": "nope"}, headers=h).status_code
        == 400
    )
    assert client.patch(f"/v1/lines/{unknown['id']}", json={"text": None}, headers=h).status_code == 400


def test_line_create_delete_and_order(client, integrations):
    h, _, e, image_ids = processed_episode(client, integrations, images=1)
    body = {"image_id": image_ids[0], "text": "추가 대사", "kind": "dialogue", "y_ratio": 0.231}
    res = client.post(f"/v1/episodes/{e['id']}/lines", json=body, headers=h)
    assert res.status_code == 201
    new = res.json()
    assert new["audio"]["status"] == "stale" and new["order"] == 2  # 같은 y_ratio의 두 번째
    existing = _line(_studio(client, h, e["id"]), text="여기가 어디지?")
    res = client.put(
        f"/v1/episodes/{e['id']}/lines/order", json={"line_ids": [new["id"], existing["id"]]}, headers=h
    )
    assert [line["order"] for line in res.json()["items"]] == [1, 2]
    assert client.delete(f"/v1/lines/{new['id']}", headers=h).status_code == 204
    assert client.delete(f"/v1/lines/{new['id']}", headers=h).status_code == 404
    bad = {**body, "image_id": "nope"}
    assert client.post(f"/v1/episodes/{e['id']}/lines", json=bad, headers=h).status_code == 400


def test_edit_blocked_while_processing(client, integrations):
    h = signup(client, "busy@example.com")
    s = create_series(client, h)
    e = create_episode(client, h, s["id"])
    image_ids = upload_images(client, h, e["id"], 1)
    client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    body = {"image_id": image_ids[0], "text": "x"}
    res = client.post(f"/v1/episodes/{e['id']}/lines", json=body, headers=h)
    assert res.status_code == 409 and res.json()["error"]["code"] == "EPISODE_STATE_INVALID"
    # 조회는 가능
    assert client.get(f"/v1/episodes/{e['id']}/studio", headers=h).status_code == 200


def test_character_voice_change_marks_lines_stale(client, integrations):
    h, s, e, _ = processed_episode(client, integrations, images=1)
    chars = client.get(f"/v1/series/{s['id']}/characters", headers=h).json()["items"]
    minjun = next(c for c in chars if c["name"] == "민준")
    res = client.patch(f"/v1/characters/{minjun['id']}", json={"name": "민준(개명)"}, headers=h)
    assert res.status_code == 200
    assert _line(_studio(client, h, e["id"]), text="여기가 어디지?")["audio"]["status"] == "ready"
    res = client.patch(
        f"/v1/characters/{minjun['id']}", json={"voice_id": "minsu", "voice_params": {"speed": 1}}, headers=h
    )
    assert res.json()["voice_id"] == "minsu"
    assert _line(_studio(client, h, e["id"]), text="여기가 어디지?")["audio"]["status"] == "stale"
    # 나레이션 인물 목소리 변경 → 화자 미지정 나레이션도 stale
    narr = next(c for c in chars if c["kind"] == "narration")
    client.patch(f"/v1/characters/{narr['id']}", json={"voice_id": "mijin"}, headers=h)
    assert _line(_studio(client, h, e["id"]), kind="narration")["audio"]["status"] == "stale"


def test_character_crud_merge(client, integrations):
    h, s, e, _ = processed_episode(client, integrations, images=1)
    res = client.post(
        f"/v1/series/{s['id']}/characters", json={"name": "민준2", "voice_id": "minsu"}, headers=h
    )
    assert res.status_code == 201
    dup = res.json()
    chars = client.get(f"/v1/series/{s['id']}/characters", headers=h).json()["items"]
    minjun = next(c for c in chars if c["name"] == "민준")
    assert (
        client.delete(f"/v1/characters/{minjun['id']}", headers=h).json()["error"]["code"]
        == "CHARACTER_IN_USE"
    )
    res = client.post("/v1/characters/merge", json={"from": minjun["id"], "into": dup["id"]}, headers=h)
    assert res.status_code == 200 and res.json()["id"] == dup["id"]
    line = _line(_studio(client, h, e["id"]), text="여기가 어디지?")
    assert line["character_id"] == dup["id"] and line["audio"]["status"] == "stale"
    assert (
        client.post(
            "/v1/characters/merge", json={"from": dup["id"], "into": dup["id"]}, headers=h
        ).status_code
        == 400
    )
    empty = client.post(f"/v1/series/{s['id']}/characters", json={"name": "엑스트라"}, headers=h).json()
    assert client.delete(f"/v1/characters/{empty['id']}", headers=h).status_code == 204
    other = signup(client, "other@example.com")
    assert client.patch(f"/v1/characters/{dup['id']}", json={"name": "x"}, headers=other).status_code == 403


def test_sfx_cues_crud(client, integrations):
    h, _, e, image_ids = processed_episode(client, integrations, images=1)
    body = {"image_id": image_ids[0], "y_ratio": 0.245, "sfx_tag": "door_open", "volume": 0.8}
    res = client.post(f"/v1/episodes/{e['id']}/sfx-cues", json=body, headers=h)
    assert res.status_code == 201
    cue = res.json()
    assert cue["duration_ms"] == 900 and cue["url"]
    default = client.post(f"/v1/episodes/{e['id']}/sfx-cues", json={**body, "volume": None}, headers=h)
    assert default.status_code == 400
    no_volume = {k: v for k, v in body.items() if k != "volume"}
    assert client.post(f"/v1/episodes/{e['id']}/sfx-cues", json=no_volume, headers=h).json()["volume"] == 1.0
    res = client.patch(f"/v1/sfx-cues/{cue['id']}", json={"volume": 0.3}, headers=h)
    assert res.json()["volume"] == 0.3 and res.json()["asset_id"] == cue["asset_id"]
    res = client.patch(f"/v1/sfx-cues/{cue['id']}", json={"sfx_tag": "footsteps"}, headers=h)
    assert res.json()["duration_ms"] == 1500 and res.json()["asset_id"] != cue["asset_id"]
    assert client.patch(f"/v1/sfx-cues/{cue['id']}", json={"duration_ms": 1}, headers=h).status_code == 400
    assert client.patch(f"/v1/sfx-cues/{cue['id']}", json={"sfx_tag": "nope"}, headers=h).status_code == 400
    assert client.delete(f"/v1/sfx-cues/{cue['id']}", headers=h).status_code == 204
    assert client.delete(f"/v1/sfx-cues/{cue['id']}", headers=h).status_code == 404


def test_catalog(client, integrations):
    h = signup(client, "cat@example.com")
    voices = client.get("/v1/voices", headers=h).json()["items"]
    assert {"id", "name", "gender", "age", "sample_url"} <= set(voices[0])
    presets = client.get("/v1/sfx-presets", headers=h).json()["items"]
    assert {"tag", "name", "category", "sample_url"} <= set(presets[0])
    assert client.get("/v1/voices").status_code == 401


def test_line_audio_redirect(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _line(_studio(client, h, e["id"]), text="여기가 어디지?")
    res = client.get(f"/v1/lines/{line['id']}/audio", headers=h, follow_redirects=False)
    assert res.status_code == 302 and "/_fake-storage/audio/" in res.headers["location"]
    sfx_line = _line(_studio(client, h, e["id"]), kind="sfx_text")
    assert client.get(f"/v1/lines/{sfx_line['id']}/audio", headers=h).status_code == 404


def test_review_marks_and_issue_exclusion(client, integrations):
    h, _, e, image_ids = processed_episode(client, integrations, images=1)
    before = client.get(f"/v1/episodes/{e['id']}?include=stats", headers=h).json()["stats"]
    assert before["issue_breakdown"]["speaker_unknown"] == 1
    res = client.patch(
        f"/v1/episodes/{e['id']}/images/{image_ids[0]}", json={"reviewed": True, "alt": "설명"}, headers=h
    )
    assert res.json()["reviewed"] is True and res.json()["reviewed_at"] and res.json()["alt"] == "설명"
    after = client.get(f"/v1/episodes/{e['id']}?include=stats", headers=h).json()["stats"]
    assert after["issue_breakdown"]["speaker_unknown"] == 0
    assert after["reviewed_image_count"] == 1 and after["review_progress"] == 1.0
    # 검수 후 수정해도 reviewed는 유지되고 stale은 계속 집계된다
    line = _line(_studio(client, h, e["id"]), text="여기가 어디지?")
    client.patch(f"/v1/lines/{line['id']}", json={"text": "바뀜"}, headers=h)
    stats = client.get(f"/v1/episodes/{e['id']}?include=stats", headers=h).json()["stats"]
    assert stats["reviewed_image_count"] == 1 and stats["issue_breakdown"]["audio_stale"] == 1
    res = client.patch(
        f"/v1/episodes/{e['id']}/images/{image_ids[0]}", json={"reviewed": False, "alt": ""}, headers=h
    )
    assert res.json()["reviewed_at"] is None and res.json()["alt"] is None


@pytest.mark.parametrize(
    ("kind", "conf", "char", "status", "reviewed", "flags"),
    [
        ("dialogue", 0.95, "c1", "ready", False, []),
        ("dialogue", 0.5, "c1", "ready", False, ["low_ocr_confidence"]),
        ("dialogue", 0.5, None, "ready", False, ["low_ocr_confidence", "speaker_unknown"]),
        ("dialogue", 0.5, None, "ready", True, []),
        ("dialogue", 0.9, None, "failed", True, ["audio_failed"]),
        ("narration", 0.9, None, "stale", True, ["audio_stale"]),
        ("narration", 0.9, None, "ready", False, []),
        ("ignore", 0.1, None, None, False, []),
        ("sfx_text", 0.5, None, None, False, ["low_ocr_confidence"]),
        ("dialogue", None, "c1", "ready", False, []),
    ],
)
def test_line_flags_table(kind, conf, char, status, reviewed, flags):
    line = DialogueLine(kind=kind, ocr_confidence=conf, character_id=char, audio_status=status)
    assert line_flags(line, reviewed, threshold=0.8) == flags
