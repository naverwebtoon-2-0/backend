from tests.conftest import signup
from tests.helpers import processed_episode
from tests.test_publish_public import _review_all


def test_reader_settings(client, integrations):
    h = signup(client, "reader@example.com", author=False)
    res = client.get("/v1/me/settings", headers=h)
    assert res.status_code == 200
    assert res.json()["reader"] == {
        "autoplay": True,
        "dialogue_volume": 1.0,
        "sfx_volume": 1.0,
        "narration": True,
        "show_subtitles": True,
        "playback_rate": 1.0,
        "high_contrast": False,
    }
    res = client.patch(
        "/v1/me/settings", json={"reader": {"sfx_volume": 0.6, "high_contrast": True}}, headers=h
    )
    assert res.json()["reader"]["sfx_volume"] == 0.6 and res.json()["reader"]["autoplay"] is True
    assert client.get("/v1/me", headers=h).json()["settings"]["reader"]["high_contrast"] is True
    assert client.patch("/v1/me/settings", json={"reader": {"sfx_volume": 2}}, headers=h).status_code == 400
    assert client.patch("/v1/me/settings", json={"reader": {"unknown": 1}}, headers=h).status_code == 400
    assert client.get("/v1/me/settings").status_code == 401


def test_quota_view(client, integrations):
    h, _, _, _ = processed_episode(client, integrations, images=2)
    quota = client.get("/v1/me/quota", headers=h).json()
    assert quota["ocr_images"] == {"used": 2, "limit": 500}
    assert quota["dubbing_chars"]["used"] > 0 and quota["dubbing_chars"]["limit"] == 100000
    assert client.get("/v1/me/quota").status_code == 401


def test_reading_progress(client, integrations):
    h, _, e, image_ids = processed_episode(client, integrations, images=1)
    reader = signup(client, "reader2@example.com", author=False)
    url = f"/v1/me/progress/{e['id']}"
    # 게시 전 회차는 404
    assert client.put(url, json={"image_index": 0, "y_ratio": 0.4}, headers=reader).status_code == 404
    _review_all(client, h, e["id"], image_ids)
    client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h)
    assert client.get(url, headers=reader).status_code == 404
    res = client.put(url, json={"image_index": 3, "y_ratio": 0.42}, headers=reader)
    assert res.status_code == 200 and res.json()["image_index"] == 3
    client.put(url, json={"image_index": 4, "y_ratio": 0.1}, headers=reader)
    got = client.get(url, headers=reader).json()
    assert (got["image_index"], got["y_ratio"]) == (4, 0.1)
    assert client.put(url, json={"image_index": -1, "y_ratio": 0.1}, headers=reader).status_code == 400
    assert client.put(url, json={"image_index": 0, "y_ratio": 0.1}).status_code == 401
