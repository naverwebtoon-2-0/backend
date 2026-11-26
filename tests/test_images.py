from app.integrations.http import UpstreamError
from tests.conftest import create_episode, create_series, make_png, signup, upload_images


def _setup(client):
    h = signup(client, "img@example.com")
    s = create_series(client, h)
    e = create_episode(client, h, s["id"])
    return h, s, e


def test_presign_complete_flow(client):
    h, _, e = _setup(client)
    ids = upload_images(client, h, e["id"], 3)
    episode = client.get(f"/v1/episodes/{e['id']}", headers=h).json()
    assert episode["state"] == "uploaded" and episode["image_count"] == 3
    items = client.get(f"/v1/episodes/{e['id']}/images", headers=h).json()["items"]
    assert [i["id"] for i in items] == ids
    assert [i["index"] for i in items] == [0, 1, 2]
    assert items[0]["width"] == 800 and items[0]["height"] == 1200
    assert items[0]["reviewed"] is False and items[0]["reviewed_at"] is None


def test_upload_completion_reports_storage_failure(client, integrations, monkeypatch):
    h, _, episode = _setup(client)
    uri = f"/v1/episodes/{episode['id']}/images"
    upload = client.post(
        f"{uri}/presign",
        json={"files": [{"filename": "a.png", "content_type": "image/png", "size": 10}]},
        headers=h,
    ).json()["uploads"][0]

    def denied(key):
        raise UpstreamError(
            "object_storage", "Access Denied", False, status=403, code="AccessDenied", operation="head_object"
        )

    monkeypatch.setattr(integrations.storage, "head", denied)
    res = client.post(f"{uri}/complete", json={"image_ids": [upload["image_id"]]}, headers=h)
    error = res.json()["error"]
    assert res.status_code == 502 and error["code"] == "STORAGE_UPSTREAM_ERROR"
    assert error["details"]["request_id"] == res.headers["X-Request-ID"]
    assert error["details"]["upstream"]["status"] == 403
    assert error["details"]["upstream"]["code"] == "AccessDenied"


def test_presign_rejects_type_and_size(client):
    h, _, e = _setup(client)
    url = f"/v1/episodes/{e['id']}/images/presign"
    res = client.post(
        url, json={"files": [{"filename": "a.gif", "content_type": "image/gif", "size": 10}]}, headers=h
    )
    assert res.status_code == 415 and res.json()["error"]["code"] == "UNSUPPORTED_MEDIA"
    big = 20 * 1024 * 1024 + 1
    res = client.post(
        url, json={"files": [{"filename": "a.png", "content_type": "image/png", "size": big}]}, headers=h
    )
    assert res.status_code == 413 and res.json()["error"]["code"] == "FILE_TOO_LARGE"
    many = [{"filename": f"{i}.png", "content_type": "image/png", "size": 10} for i in range(101)]
    assert client.post(url, json={"files": many}, headers=h).status_code == 400


def test_complete_verifies_object(client, integrations):
    h, _, e = _setup(client)
    png = make_png()
    spec = {"filename": "a.png", "content_type": "image/png", "size": len(png)}
    upload = client.post(f"/v1/episodes/{e['id']}/images/presign", json={"files": [spec]}, headers=h).json()[
        "uploads"
    ][0]
    complete = f"/v1/episodes/{e['id']}/images/complete"
    # 업로드하지 않음 → 400
    res = client.post(complete, json={"image_ids": [upload["image_id"]]}, headers=h)
    assert res.status_code == 400 and res.json()["error"]["details"]["reason"] == "object_not_found"
    # PNG라고 신청하고 다른 내용(크기는 같음)을 올림 → 415
    key = upload["url"].split("/_fake-storage/")[1].split("?")[0]
    integrations.storage.put(key, b"x" * len(png), "image/png")
    assert client.post(complete, json={"image_ids": [upload["image_id"]]}, headers=h).status_code == 415
    # 크기 불일치 → 400
    integrations.storage.put(key, png + b"extra", "image/png")
    res = client.post(complete, json={"image_ids": [upload["image_id"]]}, headers=h)
    assert res.json()["error"]["details"]["reason"] == "size_mismatch"
    # 모르는 id → 400
    assert client.post(complete, json={"image_ids": ["nope"]}, headers=h).status_code == 400


def test_fake_storage_signature(client):
    h, _, e = _setup(client)
    spec = {"filename": "a.png", "content_type": "image/png", "size": 10}
    upload = client.post(f"/v1/episodes/{e['id']}/images/presign", json={"files": [spec]}, headers=h).json()[
        "uploads"
    ][0]
    tampered = upload["url"].replace("sig=", "sig=00")
    assert client.put(tampered, content=b"x").status_code == 403


def test_reorder_and_delete(client):
    h, _, e = _setup(client)
    ids = upload_images(client, h, e["id"], 3)
    new_order = [ids[2], ids[0], ids[1]]
    res = client.put(f"/v1/episodes/{e['id']}/images/order", json={"image_ids": new_order}, headers=h)
    assert res.status_code == 200
    assert [i["id"] for i in res.json()["items"]] == new_order
    bad = client.put(f"/v1/episodes/{e['id']}/images/order", json={"image_ids": ids[:2]}, headers=h)
    assert bad.status_code == 400
    assert client.delete(f"/v1/episodes/{e['id']}/images/{ids[2]}", headers=h).status_code == 204
    items = client.get(f"/v1/episodes/{e['id']}/images", headers=h).json()["items"]
    assert [(i["id"], i["index"]) for i in items] == [(ids[0], 0), (ids[1], 1)]
    assert client.delete(f"/v1/episodes/{e['id']}/images/nope", headers=h).status_code == 404
    for image_id in ids[:2]:
        client.delete(f"/v1/episodes/{e['id']}/images/{image_id}", headers=h)
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] == "draft"


def test_review_patch_requires_editable_state(client):
    h, _, e = _setup(client)
    ids = upload_images(client, h, e["id"], 1)
    res = client.patch(f"/v1/episodes/{e['id']}/images/{ids[0]}", json={"reviewed": True}, headers=h)
    assert res.status_code == 409 and res.json()["error"]["code"] == "EPISODE_STATE_INVALID"
    assert client.patch(f"/v1/episodes/{e['id']}/images/{ids[0]}", json={}, headers=h).status_code == 400


def test_image_permissions(client):
    _, _, e = _setup(client)
    other = signup(client, "other@example.com")
    spec = {"filename": "a.png", "content_type": "image/png", "size": 10}
    assert (
        client.post(
            f"/v1/episodes/{e['id']}/images/presign", json={"files": [spec]}, headers=other
        ).status_code
        == 403
    )
    assert client.get(f"/v1/episodes/{e['id']}/images", headers=other).status_code == 403
