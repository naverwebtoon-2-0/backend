"""작업(jobs) 생성·취소. 회차당 진행 중 작업 1개는 부분 유니크 인덱스가 최종 보장한다."""

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import job_in_progress
from app.models import DialogueLine, Episode, Job, User
from app.services.episodes import ensure_no_active_job, mark_lines_stale, utcnow


def create_job(
    db: Session, episode: Episode, *, kind: str, step: str, payload: dict, user: User | None
) -> Job:
    ensure_no_active_job(db, episode.id)
    job = Job(
        episode_id=episode.id,
        kind=kind,
        status="queued",
        step=step,
        payload=payload,
        created_by=user.id if user else None,
    )
    db.add(job)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise job_in_progress() from exc
    return job


def finish_canceled(db: Session, job: Job, episode: Episode) -> None:
    """취소 마무리. 이미 만든 결과는 유지하고, 생성 중이던 대사는 재생성 필요(stale)로 되돌린다."""
    job.status = "canceled"
    job.finished_at = utcnow()
    job.error = {"code": "CANCELED", "message": "작업이 취소되었습니다.", "details": None}
    release_generating(db, job)
    if job.kind == "process":
        # 이어서 실행할 수 있게 failed(진행분 있음) 또는 uploaded(진행분 없음)로 둔다.
        episode.state = "failed" if episode.pipeline_last_step else "uploaded"
    episode.updated_at = utcnow()


def release_generating(db: Session, job: Job) -> None:
    stmt = select(DialogueLine).where(
        DialogueLine.episode_id == job.episode_id, DialogueLine.audio_status == "generating"
    )
    if job.kind == "regenerate":
        stmt = stmt.where(DialogueLine.id.in_(job.payload.get("line_ids", [])))
    mark_lines_stale(db.scalars(stmt))


def request_cancel(db: Session, job: Job, episode: Episode) -> None:
    if job.status == "queued":
        finish_canceled(db, job, episode)
    elif job.status == "running":
        db.execute(update(Job).where(Job.id == job.id).values(cancel_requested=True))
        job.cancel_requested = True
