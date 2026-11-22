from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import update

from app.db import get_sessionmaker
from app.models import Episode
from app.services.episodes import utcnow
from app.worker.scheduler import publish_due_episodes
from tests.helpers import processed_episode, run_jobs


def _review_all(client, h, episode_id, image_ids):
    for image_id in image_ids:
        res = client.patch(f"/v1/episodes/{episode_id}/images/{image_id}", json={"reviewed": True}, headers=h)
        assert res.status_code == 200


def _ready_episode(client, integrations, images=2, email="pub@example.com"):
    h, s, e, image_ids = processed_episode(client, integrations, images=images, email=email)
    _review_all(client, h, e["id"], image_ids)
    return h, s, e, image_ids


def _first_line(client, h, episode_id, text="여기가 어디지?"):
    lines = client.get(f"/v1/episodes/{episode_id}/lines", headers=h).json()["items"]
    return next(line for line in lines if line["text"] == text)


def test_publish_requires_review(client, integrations):
    h, _, e, image_ids = processed_episode(client, integrations, images=2)
    res = client.post(f"/v1/episodes/{e['id']}/publish", json={"publish_at": None}, headers=h)
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "REVIEW_INCOMPLETE" and err["details"]["unreviewed_image_ids"] == image_ids
    res = client.post(f"/v1/episodes/{e['id']}/publish", json={"allow_unreviewed": True}, headers=h)
    assert res.status_code == 200 and res.json()["state"] == "published"


def test_publish_blocked_by_stale_lines(client, integrations):
    h, _, e, _ = _ready_episode(client, integrations)
    line = _first_line(client, h, e["id"])
    client.patch(f"/v1/lines/{line['id']}", json={"text": "수정"}, headers=h)
    res = client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h)
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "EPISODE_STATE_INVALID" and err["details"]["stale_line_ids"] == [line["id"]]


def test_publish_state_rules(client, integrations):
    from tests.conftest import create_episode, create_series, signup

    h = signup(client, "rules@example.com")
    s = create_series(client, h)
    e = create_episode(client, h, s["id"])
    res = client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h)
    assert res.status_code == 409 and res.json()["error"]["code"] == "EPISODE_STATE_INVALID"
    assert client.post(f"/v1/episodes/{e['id']}/unpublish", headers=h).status_code == 409


def test_scheduled_publish(client, integrations):
    h, _, e, _ = _ready_episode(client, integrations)
    at = (utcnow() + timedelta(hours=1)).isoformat()
    res = client.post(f"/v1/episodes/{e['id']}/publish", json={"publish_at": at}, headers=h)
    assert res.status_code == 200
    assert (
        res.json()["state"] == "scheduled"
        and res.json()["scheduled_at"]
        and res.json()["published_at"] is None
    )
    assert client.get(f"/v1/public/episodes/{e['id']}").status_code == 404
    factory = get_sessionmaker()
    with factory() as s:
        assert publish_due_episodes(s) == []
        s.execute(
            update(Episode).where(Episode.id == e["id"]).values(scheduled_at=utcnow() - timedelta(seconds=1))
        )
        s.commit()
    with factory() as s:
        assert publish_due_episodes(s) == [e["id"]]
    episode = client.get(f"/v1/episodes/{e['id']}", headers=h).json()
    assert episode["state"] == "published" and episode["published_at"] and episode["scheduled_at"] is None
    assert client.get(f"/v1/public/episodes/{e['id']}").status_code == 200
    # 게시된 회차를 예약으로 돌리는 것은 거부
    at = (utcnow() + timedelta(hours=1)).isoformat()
    assert (
        client.post(f"/v1/episodes/{e['id']}/publish", json={"publish_at": at}, headers=h).status_code == 409
    )


def test_unpublish_scheduled(client, integrations):
    h, _, e, _ = _ready_episode(client, integrations)
    at = (utcnow() + timedelta(hours=1)).isoformat()
    client.post(f"/v1/episodes/{e['id']}/publish", json={"publish_at": at}, headers=h)
    res = client.post(f"/v1/episodes/{e['id']}/unpublish", headers=h)
    assert res.json()["state"] == "needs_review" and res.json()["scheduled_at"] is None


def test_public_endpoints(client, integrations):
    h, s, e, _ = _ready_episode(client, integrations)
    client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h)

    listing = client.get("/v1/public/series").json()
    assert [x["id"] for x in listing["items"]] == [s["id"]]
    assert listing["items"][0]["episode_count"] == 1 and listing["items"][0]["genre"] == "fantasy"
    assert client.get("/v1/public/series?genre=romance").json()["items"] == []
    assert client.get("/v1/public/series?q=별빛").json()["items"][0]["id"] == s["id"]
    assert client.get("/v1/public/series?q=%25").json()["items"] == []
    assert client.get("/v1/public/series?serial_status=completed").json()["items"] == []
    assert client.get("/v1/public/series?genre=horror").status_code == 400

    detail = client.get(f"/v1/public/series/{s['id']}")
    assert detail.status_code == 200 and detail.headers["cache-control"] == "private, no-store"
    eps = client.get(f"/v1/public/series/{s['id']}/episodes").json()["items"]
    assert [x["id"] for x in eps] == [e["id"]] and eps[0]["audio_duration_ms"] > 0

    ep = client.get(f"/v1/public/episodes/{e['id']}").json()
    assert ep["language"] == "ko" and ep["prev_episode_id"] is None and ep["next_episode_id"] is None
    assert ep["images"][0]["alt"] == "" and ep["images"][0]["url"]
    cues = ep["playback"]["cues"]
    keys = [(c["image_index"], c["y_ratio"]) for c in cues]
    assert keys == sorted(keys)
    voice = next(c for c in cues if c["kind"] == "dialogue" and c["text"] == "여기가 어디지?")
    assert voice["speaker"] == "민준" and voice["audio_url"] and voice["duration_ms"]
    sfx = next(c for c in cues if c["kind"] == "sfx")
    assert sfx["label"] == "천둥(가까이)" and sfx["volume"] == 1.0 and "text" not in sfx
    assert ep["playback"]["urls_expire_at"]

    res = client.get(f"/v1/public/episodes/{e['id']}/cues/{voice['id']}/audio", follow_redirects=False)
    assert res.status_code == 302
    assert client.get(f"/v1/public/episodes/{e['id']}/cues/nope/audio").status_code == 404

    transcript = client.get(f"/v1/public/episodes/{e['id']}/transcript").json()
    items = transcript["items"]
    assert items[0]["index"] == 1
    assert any(i["kind"] == "sfx" and i["text"] == "[천둥(가까이)]" for i in items)
    assert any(i["kind"] == "dialogue" and i["speaker"] == "민준" for i in items)


def test_public_hides_unpublished(client, integrations):
    _, s, e, _ = processed_episode(client, integrations, images=1)
    assert client.get(f"/v1/public/episodes/{e['id']}").status_code == 404
    assert client.get(f"/v1/public/episodes/{e['id']}/transcript").status_code == 404
    assert client.get(f"/v1/public/series/{s['id']}/episodes").status_code == 404
    assert client.get("/v1/public/series").json()["items"] == []


def test_prev_next_navigation(client, integrations):
    from tests.conftest import create_episode, upload_images

    h, s, e1, _ = _ready_episode(client, integrations, images=1)
    client.post(f"/v1/episodes/{e1['id']}/publish", json={}, headers=h)
    e2 = create_episode(client, h, s["id"], "2화")
    ids = upload_images(client, h, e2["id"], 1)
    client.post(f"/v1/episodes/{e2['id']}/process", json={}, headers=h)
    run_jobs(integrations)
    _review_all(client, h, e2["id"], ids)
    client.post(f"/v1/episodes/{e2['id']}/publish", json={}, headers=h)
    assert client.get(f"/v1/public/episodes/{e1['id']}").json()["next_episode_id"] == e2["id"]
    assert client.get(f"/v1/public/episodes/{e2['id']}").json()["prev_episode_id"] == e1["id"]


def test_preview(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    res = client.get(f"/v1/episodes/{e['id']}/preview", headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["playback"]["version"] == 0 and body["playback"]["cues"]
    assert client.get(f"/v1/episodes/{e['id']}/preview").status_code == 401


def test_dashboard(client, integrations):
    h, s, e, _ = _ready_episode(client, integrations, images=1)
    client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h)
    month = utcnow().astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m")
    res = client.get(f"/v1/series/{s['id']}/dashboard?month={month}", headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["series"]["genre"] == "fantasy"
    summary = body["summary"]
    assert summary["timezone"] == "Asia/Seoul" and summary["episode_count"] == 1
    assert summary["published_count"] == 1 and summary["published_this_month"] == 1
    assert summary["in_progress_count"] == 0
    stats = body["episodes"][0]["stats"]
    assert set(stats["issue_breakdown"]) == {
        "low_ocr_confidence",
        "speaker_unknown",
        "audio_failed",
        "audio_stale",
    }
    assert (
        client.get(f"/v1/series/{s['id']}/dashboard?month=1999-01", headers=h).json()["summary"][
            "published_this_month"
        ]
        == 0
    )
    assert client.get(f"/v1/series/{s['id']}/dashboard?month=2026-13", headers=h).status_code == 400


def test_dashboard_month_boundary_uses_seoul(client, integrations, db):
    h, s, e, _ = _ready_episode(client, integrations, images=1)
    client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h)
    # 2026-10-31 16:30 UTC = 2026-11-01 01:30 KST → 11월로 집계
    from datetime import UTC, datetime

    db.execute(
        update(Episode)
        .where(Episode.id == e["id"])
        .values(published_at=datetime(2026, 10, 31, 16, 30, tzinfo=UTC))
    )
    db.commit()
    nov = client.get(f"/v1/series/{s['id']}/dashboard?month=2026-11", headers=h).json()["summary"]
    oct_ = client.get(f"/v1/series/{s['id']}/dashboard?month=2026-10", headers=h).json()["summary"]
    assert nov["published_this_month"] == 1 and oct_["published_this_month"] == 0
