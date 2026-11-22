from datetime import timedelta

from sqlalchemy import update

from app.db import get_sessionmaker
from app.integrations import Integrations
from app.models import Job
from app.services.episodes import utcnow
from app.worker.pipeline import JobRunner, claim_job
from tests.conftest import create_episode, create_series, signup, upload_images


def run_jobs(integrations: Integrations, limit: int = 20) -> int:
    """대기 작업을 모두 실행한다 (워커 1회분씩)."""
    factory = get_sessionmaker()
    runner = JobRunner(factory, integrations)
    count = 0
    for _ in range(limit):
        with factory() as s:
            job_id = claim_job(s)
        if job_id is None:
            break
        runner.run(job_id)
        count += 1
    return count


def processed_episode(client, integrations, images: int = 2, email: str = "author@example.com"):
    """가입 → 작품 → 회차 → 업로드 → process → 워커 실행까지 마친 회차."""
    h = signup(client, email)
    s = create_series(client, h)
    e = create_episode(client, h, s["id"])
    image_ids = upload_images(client, h, e["id"], images)
    res = client.post(f"/v1/episodes/{e['id']}/process", json={}, headers=h)
    assert res.status_code == 202, res.text
    run_jobs(integrations)
    return h, s, e, image_ids


def age_heartbeat(job_id: str, seconds: int) -> None:
    with get_sessionmaker()() as s:
        s.execute(
            update(Job).where(Job.id == job_id).values(heartbeat_at=utcnow() - timedelta(seconds=seconds))
        )
        s.commit()
