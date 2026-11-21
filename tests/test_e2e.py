"""Fake 어댑터만으로 전체 흐름:
회원가입 → 작품·회차 생성 → 업로드(presign/complete) → process → 스튜디오 수정 → 재생성 → 게시 → 독자 조회
"""

from tests.conftest import make_png
from tests.helpers import run_jobs


def test_full_flow(client, integrations):
    # 1) 작가 가입·로그인
    res = client.post(
        "/v1/auth/signup",
        json={
            "email": "flow@example.com",
            "password": "password123",
            "nickname": "작가",
            "invite_code": "invite-123",
        },
    )
    assert res.status_code == 201 and res.json()["user"]["role"] == "author"
    tokens = client.post(
        "/v1/auth/login", json={"email": "flow@example.com", "password": "password123"}
    ).json()
    h = {"Authorization": f"Bearer {tokens['access_token']}"}

    # 2) 작품·회차 생성
    series = client.post(
        "/v1/series", json={"title": "몰입 웹툰", "genre": "drama"}, headers={**h, "Idempotency-Key": "s-1"}
    ).json()
    episode = client.post(
        f"/v1/series/{series['id']}/episodes", json={"title": "1화"}, headers={**h, "Idempotency-Key": "e-1"}
    ).json()
    assert episode["state"] == "draft"

    # 3) 업로드: presign → Object Storage PUT → complete
    files = [make_png(800, 2000), make_png(800, 2400)]
    uploads = client.post(
        f"/v1/episodes/{episode['id']}/images/presign",
        json={
            "files": [
                {"filename": f"{i}.png", "content_type": "image/png", "size": len(f)}
                for i, f in enumerate(files)
            ]
        },
        headers=h,
    ).json()["uploads"]
    for upload, data in zip(uploads, files, strict=True):
        assert client.put(upload["url"], content=data, headers=upload["headers"]).status_code == 200
    image_ids = [u["image_id"] for u in uploads]
    done = client.post(
        f"/v1/episodes/{episode['id']}/images/complete", json={"image_ids": image_ids}, headers=h
    )
    assert done.json()["episode"]["state"] == "uploaded" and done.json()["episode"]["image_count"] == 2

    # 4) 분석·음성 생성
    res = client.post(
        f"/v1/episodes/{episode['id']}/process", json={}, headers={**h, "Idempotency-Key": "p-1"}
    )
    assert res.status_code == 202
    job_id = res.json()["job"]["id"]
    run_jobs(integrations)
    assert client.get(f"/v1/jobs/{job_id}", headers=h).json()["status"] == "succeeded"

    # 5) 스튜디오 수정: 화자 지정, 대사 수정, 효과음 추가, 검수 표시·alt
    studio = client.get(f"/v1/episodes/{episode['id']}/studio", headers=h).json()
    assert studio["episode"]["state"] == "needs_review"
    minjun = next(c for c in studio["characters"] if c["name"] == "민준")
    unknown = next(line for line in studio["lines"] if line["text"] == "누구야!")
    res = client.patch(
        f"/v1/lines/{unknown['id']}", json={"character_id": minjun["id"], "text": "누구세요!"}, headers=h
    )
    assert res.json()["audio"]["status"] == "stale"
    res = client.post(
        f"/v1/episodes/{episode['id']}/sfx-cues",
        json={"image_id": image_ids[0], "y_ratio": 0.6, "sfx_tag": "footsteps", "volume": 0.7},
        headers=h,
    )
    assert res.status_code == 201
    for i, image_id in enumerate(image_ids):
        client.patch(
            f"/v1/episodes/{episode['id']}/images/{image_id}",
            json={"reviewed": True, "alt": f"{i + 1}번째 장면"},
            headers=h,
        )

    # stale 대사가 남아 있으면 게시 불가
    res = client.post(f"/v1/episodes/{episode['id']}/publish", json={}, headers=h)
    assert res.status_code == 409 and unknown["id"] in res.json()["error"]["details"]["stale_line_ids"]

    # 6) 재생성
    res = client.post(f"/v1/episodes/{episode['id']}/regenerate", json={"scope": "stale"}, headers=h)
    assert res.status_code == 202
    run_jobs(integrations)
    stats = client.get(f"/v1/episodes/{episode['id']}?include=stats", headers=h).json()["stats"]
    assert stats["issue_breakdown"]["audio_stale"] == 0 and stats["review_progress"] == 1.0

    # 7) 게시
    res = client.post(
        f"/v1/episodes/{episode['id']}/publish",
        json={"publish_at": None, "allow_unreviewed": False},
        headers={**h, "Idempotency-Key": "pub-1"},
    )
    assert res.status_code == 200 and res.json()["state"] == "published"

    # 8) 독자 조회 (비로그인)
    listing = client.get("/v1/public/series?genre=drama").json()["items"]
    assert [s["id"] for s in listing] == [series["id"]]
    viewer = client.get(f"/v1/public/episodes/{episode['id']}").json()
    assert [img["alt"] for img in viewer["images"]] == ["1번째 장면", "2번째 장면"]
    cues = viewer["playback"]["cues"]
    edited = next(c for c in cues if c.get("text") == "누구세요!")
    assert edited["speaker"] == "민준" and edited["audio_url"]
    steps = next(c for c in cues if c["kind"] == "sfx" and c["label"] == "발소리")
    assert steps["volume"] == 0.7
    assert client.get(steps["audio_url"]).status_code == 200  # 서명 URL로 음원 다운로드
    transcript = client.get(f"/v1/public/episodes/{episode['id']}/transcript").json()["items"]
    assert any(item["text"] == "[발소리]" for item in transcript)

    # 독자 계정: 설정·진행 위치
    client.post(
        "/v1/auth/signup", json={"email": "fan@example.com", "password": "password123", "nickname": "독자"}
    )
    reader_token = client.post(
        "/v1/auth/login", json={"email": "fan@example.com", "password": "password123"}
    ).json()
    rh = {"Authorization": f"Bearer {reader_token['access_token']}"}
    assert (
        client.put(
            f"/v1/me/progress/{episode['id']}", json={"image_index": 1, "y_ratio": 0.5}, headers=rh
        ).status_code
        == 200
    )
    assert (
        client.patch("/v1/me/settings", json={"reader": {"sfx_volume": 0.4}}, headers=rh).status_code == 200
    )
