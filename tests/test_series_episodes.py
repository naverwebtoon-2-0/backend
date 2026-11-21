from tests.conftest import create_episode, create_series, make_png, signup


def test_series_crud(client):
    h = signup(client, "s@example.com")
    s = create_series(client, h)
    assert s["genre"] == "fantasy" and s["serial_status"] == "ongoing" and s["episode_count"] == 0
    # 시각은 UTC(Z)로 통일 (생성 응답과 조회 응답 모두)
    assert s["created_at"].endswith("Z")
    assert client.get(f"/v1/series/{s['id']}", headers=h).json()["created_at"] == s["created_at"]
    assert client.get(f"/v1/series/{s['id']}", headers=h).json()["title"] == "별빛 아래 우리"
    res = client.patch(
        f"/v1/series/{s['id']}", json={"serial_status": "hiatus", "title": "새 제목"}, headers=h
    )
    assert res.status_code == 200 and res.json()["serial_status"] == "hiatus"
    assert client.get("/v1/series", headers=h).json()["items"][0]["id"] == s["id"]
    assert client.delete(f"/v1/series/{s['id']}", headers=h).status_code == 204
    assert client.get(f"/v1/series/{s['id']}", headers=h).status_code == 404


def test_series_validation(client):
    h = signup(client, "v@example.com")
    assert client.post("/v1/series", json={"title": "t", "genre": "horror"}, headers=h).status_code == 400
    assert client.post("/v1/series", json={"title": "t"}, headers=h).status_code == 400
    s = create_series(client, h)
    assert client.patch(f"/v1/series/{s['id']}", json={"serial_status": "done"}, headers=h).status_code == 400
    assert client.patch(f"/v1/series/{s['id']}", json={"title": None}, headers=h).status_code == 400


def test_series_permissions(client):
    owner = signup(client, "o@example.com")
    other = signup(client, "x@example.com")
    reader = signup(client, "r@example.com", author=False)
    s = create_series(client, owner)
    assert client.get(f"/v1/series/{s['id']}", headers=other).status_code == 403
    assert client.patch(f"/v1/series/{s['id']}", json={"title": "x"}, headers=other).status_code == 403
    assert client.delete(f"/v1/series/{s['id']}", headers=other).status_code == 403
    assert client.post("/v1/series", json={"title": "t", "genre": "drama"}, headers=reader).status_code == 403
    assert client.get("/v1/series", headers=reader).status_code == 403
    assert client.get("/v1/series").status_code == 401
    assert client.get("/v1/series/nope", headers=owner).status_code == 404


def test_series_pagination(client):
    h = signup(client, "p@example.com")
    ids = [create_series(client, h, title=f"작품{i}")["id"] for i in range(5)]
    page1 = client.get("/v1/series?limit=2", headers=h).json()
    page2 = client.get(f"/v1/series?limit=2&cursor={page1['next_cursor']}", headers=h).json()
    page3 = client.get(f"/v1/series?limit=2&cursor={page2['next_cursor']}", headers=h).json()
    got = [s["id"] for s in page1["items"] + page2["items"] + page3["items"]]
    assert got == list(reversed(ids))
    assert page3["next_cursor"] is None
    assert client.get("/v1/series?cursor=%%%", headers=h).status_code == 400
    assert client.get("/v1/series?limit=0", headers=h).status_code == 400


def test_idempotency_key(client):
    h = signup(client, "i@example.com")
    body = {"title": "t", "genre": "drama"}
    first = client.post("/v1/series", json=body, headers={**h, "Idempotency-Key": "k1"})
    again = client.post("/v1/series", json=body, headers={**h, "Idempotency-Key": "k1"})
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert again.headers.get("idempotent-replayed") == "true"
    assert len(client.get("/v1/series", headers=h).json()["items"]) == 1
    conflict = client.post(
        "/v1/series", json={**body, "title": "other"}, headers={**h, "Idempotency-Key": "k1"}
    )
    assert conflict.status_code == 409


def test_cover_upload(client):
    h = signup(client, "c@example.com")
    s = create_series(client, h)
    png = make_png(300, 400)
    res = client.post(
        f"/v1/series/{s['id']}/cover/presign",
        json={"filename": "cover.png", "content_type": "image/png", "size": len(png)},
        headers=h,
    )
    assert res.status_code == 200
    upload = res.json()["upload"]
    assert client.put(upload["url"], content=png, headers=upload["headers"]).status_code == 200
    res = client.post(f"/v1/series/{s['id']}/cover/complete", headers=h)
    assert res.status_code == 200
    assert res.json()["cover_url"].startswith("http://testserver/_fake-storage/covers/")
    assert client.post(f"/v1/series/{s['id']}/cover/complete", headers=h).status_code == 400


def test_episode_crud(client):
    h = signup(client, "e@example.com")
    s = create_series(client, h)
    e1 = create_episode(client, h, s["id"], "1화")
    e2 = create_episode(client, h, s["id"], "2화")
    assert (e1["number"], e2["number"]) == (1, 2)
    assert e1["state"] == "draft" and e1["published_at"] is None and e1["has_unpublished_changes"] is False
    items = client.get(f"/v1/series/{s['id']}/episodes", headers=h).json()["items"]
    assert [e["id"] for e in items] == [e1["id"], e2["id"]]
    assert "stats" not in items[0]
    with_stats = client.get(f"/v1/series/{s['id']}/episodes?include=stats", headers=h).json()["items"]
    assert with_stats[0]["stats"]["review_progress"] == 0.0
    res = client.patch(f"/v1/episodes/{e1['id']}", json={"title": "첫 화", "number": 3}, headers=h)
    assert res.json()["title"] == "첫 화" and res.json()["number"] == 3
    assert client.get(f"/v1/episodes/{e1['id']}?include=stats", headers=h).json()["stats"] is not None
    assert client.delete(f"/v1/episodes/{e2['id']}", headers=h).status_code == 204
    assert client.get(f"/v1/episodes/{e2['id']}", headers=h).status_code == 404
    assert client.get(f"/v1/series/{s['id']}/episodes?include=bogus", headers=h).status_code == 400


def test_episode_permissions(client):
    owner = signup(client, "eo@example.com")
    other = signup(client, "ex@example.com")
    s = create_series(client, owner)
    e = create_episode(client, owner, s["id"])
    assert client.get(f"/v1/episodes/{e['id']}", headers=other).status_code == 403
    assert (
        client.post(f"/v1/series/{s['id']}/episodes", json={"title": "x"}, headers=other).status_code == 403
    )
    assert client.delete(f"/v1/episodes/{e['id']}", headers=other).status_code == 403
