from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import DialogueLine
from tests.conftest import signup
from tests.helpers import processed_episode, run_jobs
from tests.test_publish_public import _first_line, _ready_episode


def _status(line_id):
    with get_sessionmaker()() as s:
        return s.get(DialogueLine, line_id).audio_status


def test_regenerate_stale_scope(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _first_line(client, h, e["id"])
    client.patch(f"/v1/lines/{line['id']}", json={"text": "새 대사"}, headers=h)
    calls = integrations.dubbing.calls
    res = client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    assert res.status_code == 202
    job = res.json()["job"]
    assert job["kind"] == "regenerate" and job["status"] == "queued"
    assert _status(line["id"]) == "generating"
    again = client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "JOB_IN_PROGRESS"
    run_jobs(integrations)
    assert client.get(f"/v1/jobs/{job['id']}", headers=h).json()["status"] == "succeeded"
    assert _status(line["id"]) == "ready"
    assert integrations.dubbing.calls == calls + 1
    # stale이 없으면 400
    res = client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    assert res.status_code == 400


def test_regenerate_lines_and_all_scope(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _first_line(client, h, e["id"])
    url = f"/v1/episodes/{e['id']}/regenerate"
    assert client.post(url, json={"scope": "lines"}, headers=h).status_code == 400
    assert client.post(url, json={"scope": "lines", "line_ids": ["nope"]}, headers=h).status_code == 400
    assert client.post(url, json={"scope": "bogus"}, headers=h).status_code == 400
    calls = integrations.dubbing.calls
    assert client.post(url, json={"scope": "lines", "line_ids": [line["id"]]}, headers=h).status_code == 202
    run_jobs(integrations)
    assert integrations.dubbing.calls == calls  # 같은 입력 → 캐시 적중
    assert client.post(url, json={"scope": "all"}, headers=h).status_code == 202
    run_jobs(integrations)
    assert integrations.dubbing.calls == calls


def test_edit_during_regeneration_discards_result(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _first_line(client, h, e["id"])
    client.patch(f"/v1/lines/{line['id']}", json={"text": "첫 수정"}, headers=h)
    client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    # 작업 중 수정은 허용
    res = client.patch(f"/v1/lines/{line['id']}", json={"text": "두 번째 수정"}, headers=h)
    assert res.status_code == 200
    run_jobs(integrations)
    with get_sessionmaker()() as s:
        current = s.get(DialogueLine, line["id"])
        assert current.audio_status == "stale" and current.text == "두 번째 수정"


def test_line_regenerate_cache_hit_and_job(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _first_line(client, h, e["id"])
    url = f"/v1/lines/{line['id']}/regenerate"
    # 같은 입력 → 캐시 적중 200
    res = client.post(url, json={"force": False}, headers=h)
    assert res.status_code == 200 and res.json()["audio"]["status"] == "ready"
    # 텍스트를 바꿨다가 되돌리면 캐시 적중
    client.patch(f"/v1/lines/{line['id']}", json={"text": "다른 말"}, headers=h)
    client.patch(f"/v1/lines/{line['id']}", json={"text": "여기가 어디지?"}, headers=h)
    assert client.post(url, json={}, headers=h).status_code == 200
    # 새 입력 → 202 + job
    client.patch(f"/v1/lines/{line['id']}", json={"text": "처음 보는 말"}, headers=h)
    res = client.post(url, json={}, headers=h)
    assert res.status_code == 202 and res.json()["job"]["kind"] == "regenerate"
    run_jobs(integrations)
    assert _status(line["id"]) == "ready"
    # force → 같은 입력이어도 새로 합성
    calls = integrations.dubbing.calls
    assert client.post(url, json={"force": True}, headers=h).status_code == 202
    run_jobs(integrations)
    assert integrations.dubbing.calls == calls + 1
    items = client.get(f"/v1/episodes/{e['id']}/lines", headers=h).json()["items"]
    sfx = next(x for x in items if x["kind"] == "sfx_text")
    assert client.post(f"/v1/lines/{sfx['id']}/regenerate", json={}, headers=h).status_code == 400


def test_regenerate_retries_failed_lines(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _first_line(client, h, e["id"])
    client.patch(f"/v1/lines/{line['id']}", json={"text": "실패 [[fail]]"}, headers=h)
    client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    run_jobs(integrations)
    assert _status(line["id"]) == "failed"
    client.patch(f"/v1/lines/{line['id']}", json={"text": "이번엔 성공"}, headers=h)
    client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    run_jobs(integrations)
    assert _status(line["id"]) == "ready"


def test_regenerate_quota(client, integrations, monkeypatch):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    line = _first_line(client, h, e["id"])
    client.patch(f"/v1/lines/{line['id']}", json={"text": "아주 긴 새 대사입니다"}, headers=h)
    used = client.get("/v1/me/quota", headers=h).json()["dubbing_chars"]["used"]
    assert used > 0
    monkeypatch.setattr(get_settings(), "quota_dubbing_chars", used + 3)
    res = client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    assert res.status_code == 429 and res.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert client.post(f"/v1/lines/{line['id']}/regenerate", json={}, headers=h).status_code == 429


def test_regenerate_permissions(client, integrations):
    h, _, e, _ = processed_episode(client, integrations, images=1)
    other = signup(client, "other@example.com")
    url = f"/v1/episodes/{e['id']}/regenerate"
    assert client.post(url, json={"scope": "all"}, headers=other).status_code == 403
    line = _first_line(client, h, e["id"])
    assert client.post(f"/v1/lines/{line['id']}/regenerate", json={}, headers=other).status_code == 403
    with get_sessionmaker()() as s:
        assert s.scalars(select(DialogueLine)).first() is not None


def test_publish_snapshot_isolation_and_republish(client, integrations):
    h, s, e, _ = _ready_episode(client, integrations)
    res = client.post(
        f"/v1/episodes/{e['id']}/publish", json={"publish_at": None, "allow_unreviewed": False}, headers=h
    )
    assert res.status_code == 200
    body = res.json()
    first_published_at = body["published_at"]
    assert body["state"] == "published" and body["playback_version"] == 1 and first_published_at

    public = client.get(f"/v1/public/episodes/{e['id']}").json()
    assert public["playback"]["version"] == 1
    texts = [c.get("text") for c in public["playback"]["cues"]]
    assert "여기가 어디지?" in texts

    # 게시 후 수정 → has_unpublished_changes, 독자는 이전 스냅샷
    line = _first_line(client, h, e["id"])
    client.patch(f"/v1/lines/{line['id']}", json={"text": "여기는 어디야?"}, headers=h)
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["has_unpublished_changes"] is True
    public = client.get(f"/v1/public/episodes/{e['id']}").json()
    assert "여기는 어디야?" not in [c.get("text") for c in public["playback"]["cues"]]

    # stale 남아 있으면 재게시 불가 → 재생성 후 재게시
    assert client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h).status_code == 409
    client.post(f"/v1/episodes/{e['id']}/regenerate", json={"scope": "stale"}, headers=h)
    run_jobs(integrations)
    res = client.post(f"/v1/episodes/{e['id']}/publish", json={}, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["playback_version"] == 2 and res.json()["has_unpublished_changes"] is False
    assert res.json()["published_at"] == first_published_at
    public = client.get(f"/v1/public/episodes/{e['id']}").json()
    assert "여기는 어디야?" in [c.get("text") for c in public["playback"]["cues"]]

    # 게시 취소 → needs_review, published_at 유지, 독자에게 404
    res = client.post(f"/v1/episodes/{e['id']}/unpublish", headers=h)
    assert res.json()["state"] == "needs_review" and res.json()["published_at"] == first_published_at
    assert client.get(f"/v1/public/episodes/{e['id']}").status_code == 404
    assert client.get(f"/v1/public/series/{s['id']}").status_code == 404
