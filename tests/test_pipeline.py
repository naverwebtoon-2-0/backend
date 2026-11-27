from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.integrations.http import UpstreamError
from app.integrations.ocr import FakeOcrClient, OcrField
from app.models import DialogueLine, Episode, Job
from app.services.episodes import utcnow
from app.services.views import eta_seconds
from app.worker.pipeline import claim_job, reap_stale_jobs
from tests.conftest import create_episode, create_series, signup, upload_images
from tests.helpers import age_heartbeat, processed_episode, run_jobs


def _setup(client, images=2):
    h = signup(client, "p@example.com")
    s = create_series(client, h)
    e = create_episode(client, h, s["id"])
    image_ids = upload_images(client, h, e["id"], images)
    return h, s, e, image_ids


def test_process_end_to_end(client, integrations):
    h, s, e, _ = _setup(client, 2)
    res = client.post(
        f"/v1/episodes/{e['id']}/process",
        json={
            "from_step": None,
            "options": {"ocr_language": "ko", "narration": True, "description": True, "sfx": True},
        },
        headers=h,
    )
    assert res.status_code == 202
    body = res.json()
    assert body["job"]["status"] == "queued" and body["job"]["step"] == "ocr"
    assert body["episode"]["state"] == "analyzing"
    job_id = body["job"]["id"]

    assert run_jobs(integrations) == 1
    job = client.get(f"/v1/jobs/{job_id}", headers=h).json()
    assert job["status"] == "succeeded" and job["step"] == "done"
    assert job["finished_at"] is not None and job["error"] is None

    episode = client.get(f"/v1/episodes/{e['id']}?include=stats", headers=h).json()
    assert episode["state"] == "needs_review"
    stats = episode["stats"]
    # 이미지마다 '누구야!'는 화자 미확정 → 문제 장면 2개 이상
    assert stats["issue_breakdown"]["speaker_unknown"] == 2
    assert stats["issue_count"] >= 2
    assert stats["audio_duration_ms"] > 0
    assert stats["processing"] is None

    with get_sessionmaker()() as db:
        lines = db.scalars(select(DialogueLine).where(DialogueLine.episode_id == e["id"])).all()
        kinds = sorted({line.kind for line in lines})
        assert kinds == ["dialogue", "narration", "sfx_text"]
        assert all(line.audio_status == "ready" for line in lines if line.kind != "sfx_text")
        assert all(line.audio_status is None for line in lines if line.kind == "sfx_text")

    chars = client.get(f"/v1/series/{s['id']}/characters", headers=h)
    if chars.status_code == 200:  # M3에서 추가되는 API
        assert {"나레이션", "민준"} <= {c["name"] for c in chars.json()["items"]}

    jobs = client.get(f"/v1/episodes/{e['id']}/jobs", headers=h).json()["items"]
    assert [j["id"] for j in jobs] == [job_id]


def test_audio_cache_reuses_identical_input(client, integrations):
    _setup_h, _, _, _ = processed_episode(client, integrations, images=3)
    # 3장 모두 같은 텍스트를 내는 Fake OCR → 고유 입력(나레이션 1, 대사 2)만 합성
    assert integrations.dubbing.calls == 3


def test_process_state_rules(client, integrations):
    h = signup(client, "st@example.com")
    s = create_series(client, h)
    e = create_episode(client, h, s["id"])
    res = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    assert res.status_code == 409 and res.json()["error"]["code"] == "EPISODE_STATE_INVALID"
    upload_images(client, h, e["id"], 1)
    assert client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h).status_code == 202
    # 진행 중 작업이 있으면 같은 회차의 상태(analyzing) 때문에 409
    again = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    assert again.status_code == 409
    other = signup(client, "o2@example.com")
    assert client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=other).status_code == 403
    bad = client.post(f"/v1/episodes/{e['id']}/process", json={"from_step": "nope"}, headers=h)
    assert bad.status_code == 400


def test_job_in_progress_is_reported(client, integrations, db):
    h, _, e, _ = _setup(client, 1)
    client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    # 상태를 강제로 failed로 돌려도, 진행 중 작업이 남아 있으면 JOB_IN_PROGRESS
    episode = db.get(Episode, e["id"])
    episode.state = "failed"
    db.commit()
    res = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    assert res.status_code == 409 and res.json()["error"]["code"] == "JOB_IN_PROGRESS"


class FlakyOcr(FakeOcrClient):
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def recognize(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise UpstreamError(
                "clova_ocr", "OCR server error", status=500, code="5000", request_id="ocr-test"
            )
        return super().recognize(*args, **kwargs)


def test_ocr_failure_then_resume(client, integrations):
    h, _, e, _ = _setup(client, 2)
    integrations.ocr = FlakyOcr(fail_times=1)
    job_id = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h).json()["job"]["id"]
    run_jobs(integrations)
    job = client.get(f"/v1/jobs/{job_id}", headers=h).json()
    assert job["status"] == "failed"
    assert job["error"]["code"] == "AI_UPSTREAM_ERROR" and job["error"]["details"]["step"] == "ocr"
    assert job["error"]["details"]["upstream"]["status"] == 500
    assert job["error"]["details"]["upstream"]["request_id"] == "ocr-test"
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] == "failed"

    res = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    assert res.status_code == 202 and res.json()["job"]["step"] == "ocr"
    run_jobs(integrations)
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] == "needs_review"
    assert integrations.ocr.calls == 3  # 실패 1 + 이미지 2


class FailingStructure:
    def __init__(self, inner):
        self.inner = inner
        self.fail = True

    def structure(self, *args, **kwargs):
        if self.fail:
            raise UpstreamError("clova_studio", "HTTP 503")
        return self.inner.structure(*args, **kwargs)


def test_invalid_studio_result_preserves_ocr_and_records_warning(client, integrations):
    class InvalidResponseStructure:
        def structure(self, *args, **kwargs):
            raise UpstreamError(
                "clova_studio",
                "invalid JSON",
                False,
                status=200,
                code="INVALID_RESPONSE",
                request_id="studio-test",
            )

    h, _, episode, _ = _setup(client, 1)
    integrations.structure = InvalidResponseStructure()
    job_id = client.post(f"/v1/episodes/{episode['id']}/process", json={}, headers=h).json()["job"]["id"]
    run_jobs(integrations)
    job = client.get(f"/v1/jobs/{job_id}", headers=h).json()
    assert job["status"] == "succeeded"
    warning = job["warnings"][0]
    assert warning["details"]["step"] == "structure"
    assert warning["details"]["upstream"]["request_id"] == "studio-test"
    lines = client.get(f"/v1/episodes/{episode['id']}/lines", headers=h).json()["items"]
    assert any(
        line["original_text"] == "여기가 어디지?" and line["audio"]["status"] == "ready" for line in lines
    )


def test_resume_from_last_successful_step(client, integrations):
    h, _, e, _ = _setup(client, 2)
    ocr = FlakyOcr(fail_times=0)
    integrations.ocr = ocr
    integrations.structure = FailingStructure(integrations.structure)
    client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    run_jobs(integrations)
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] == "failed"
    assert ocr.calls == 2

    integrations.structure.fail = False
    res = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    assert res.json()["job"]["step"] == "structure"
    run_jobs(integrations)
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] == "needs_review"
    assert ocr.calls == 2  # OCR 결과 재사용

    # 처음부터 다시
    res = client.post(f"/v1/episodes/{e['id']}/process", json={"from_step": "ocr"}, headers=h)
    assert res.status_code == 409  # needs_review에서는 process 불가


class FailOneLineOcr(FakeOcrClient):
    def recognize(self, *args, **kwargs):
        fields = super().recognize(*args, **kwargs)
        fields.append(OcrField("이건 실패 [[fail]]?", {"x": 0.1, "y": 0.9, "w": 0.2, "h": 0.04}, 0.99))
        return fields


def test_partial_audio_failure_goes_to_needs_review(client, integrations):
    h, _, e, _ = _setup(client, 1)
    integrations.ocr = FailOneLineOcr()
    job_id = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h).json()["job"]["id"]
    run_jobs(integrations)
    episode = client.get(f"/v1/episodes/{e['id']}?include=stats", headers=h).json()
    assert episode["state"] == "needs_review"
    assert episode["stats"]["issue_breakdown"]["audio_failed"] == 1
    job = client.get(f"/v1/jobs/{job_id}", headers=h).json()
    warning = job["warnings"][0]
    assert job["status"] == "succeeded" and warning["details"]["service"] == "clova_voice"
    lines = client.get(f"/v1/episodes/{e['id']}/lines", headers=h).json()["items"]
    failed_line = next(line for line in lines if line["id"] == warning["details"]["line_id"])
    assert failed_line["audio"]["status"] == "failed" and failed_line["audio"]["error"] == warning
    with get_sessionmaker()() as db:
        failed = db.scalars(select(DialogueLine).where(DialogueLine.audio_status == "failed")).all()
        assert [line.text for line in failed] == ["이건 실패 [[fail]]?"]


def test_cancel_queued_job(client, integrations):
    h, _, e, _ = _setup(client, 1)
    job_id = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h).json()["job"]["id"]
    res = client.post(f"/v1/jobs/{job_id}/cancel", headers=h)
    assert res.status_code == 200 and res.json()["status"] == "canceled"
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] == "uploaded"
    assert client.post(f"/v1/jobs/{job_id}/cancel", headers=h).status_code == 409
    assert run_jobs(integrations) == 0


def test_cancel_running_job(client, integrations):
    h, _, e, _ = _setup(client, 2)
    job_id = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h).json()["job"]["id"]
    factory = get_sessionmaker()
    with factory() as s:
        assert claim_job(s) == job_id
    res = client.post(f"/v1/jobs/{job_id}/cancel", headers=h)
    assert res.status_code == 200 and res.json()["status"] == "running"
    from app.worker.pipeline import JobRunner

    JobRunner(factory, integrations).run(job_id)
    assert client.get(f"/v1/jobs/{job_id}", headers=h).json()["status"] == "canceled"
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] in ("uploaded", "failed")


def test_reap_stale_job_requeues_then_fails(client, integrations):
    h, _, e, _ = _setup(client, 1)
    job_id = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h).json()["job"]["id"]
    factory = get_sessionmaker()
    timeout = get_settings().job_heartbeat_timeout_seconds
    for attempt in range(1, 4):
        with factory() as s:
            assert claim_job(s) == job_id
        age_heartbeat(job_id, timeout + 5)
        with factory() as s:
            reap_stale_jobs(s, timeout, max_attempts=3)
        status = client.get(f"/v1/jobs/{job_id}", headers=h).json()["status"]
        assert status == ("queued" if attempt < 3 else "failed")
    job = client.get(f"/v1/jobs/{job_id}", headers=h).json()
    assert job["error"]["code"] == "WORKER_LOST"
    assert client.get(f"/v1/episodes/{e['id']}", headers=h).json()["state"] == "failed"


def test_reap_ignores_fresh_heartbeat(client, integrations):
    h, _, e, _ = _setup(client, 1)
    client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    with get_sessionmaker()() as s:
        claim_job(s)
    with get_sessionmaker()() as s:
        assert reap_stale_jobs(s, 120, 3) == []


def test_ocr_quota_exceeded(client, integrations, monkeypatch):
    h, _, e, _ = _setup(client, 2)
    monkeypatch.setattr(get_settings(), "quota_ocr_images", 1)
    res = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    assert res.status_code == 429 and res.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert client.get("/v1/me/quota", headers=h).json()["ocr_images"] == {"used": 0, "limit": 1}


def test_job_permissions(client, integrations):
    h, _, e, _ = _setup(client, 1)
    job_id = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h).json()["job"]["id"]
    other = signup(client, "x@example.com")
    assert client.get(f"/v1/jobs/{job_id}", headers=other).status_code == 403
    assert client.post(f"/v1/jobs/{job_id}/cancel", headers=other).status_code == 403
    assert client.get("/v1/jobs/nope", headers=h).status_code == 404


@pytest.mark.parametrize(
    ("status", "done", "total", "elapsed", "rates", "expected"),
    [
        ("queued", 0, 10, 0, {}, None),
        ("running", 0, 10, 5, {}, None),
        ("running", 0, 10, 5, {"ocr": 2.0}, 20),
        ("running", 5, 10, 10, {}, 10),
        ("running", 10, 10, 10, {}, 0),
        ("running", 0, 0, 0, {}, None),
    ],
)
def test_eta_seconds(status, done, total, elapsed, rates, expected):
    now = utcnow()
    job = Job(
        status=status,
        progress_done=done,
        progress_total=total,
        step_started_at=now - timedelta(seconds=elapsed),
        step_rates=rates,
    )
    assert eta_seconds(job, now) == expected
