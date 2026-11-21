from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_author
from app.errors import ApiError, state_invalid
from app.idempotency import Idempotency
from app.models import EpisodeImage, Job, User
from app.models.episode import PIPELINE_STEPS
from app.pagination import check_limit, paginate
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.episodes import JobOut, ProcessRequest, ProcessResponse
from app.services import quota
from app.services.access import episode_for_author, job_for_author
from app.services.episodes import ensure_no_active_job, utcnow
from app.services.jobs import create_job, request_cancel
from app.services.views import job_view

router = APIRouter(tags=["jobs"], responses=ERROR_RESPONSES)


def _resume_step(last: str | None) -> str:
    if last is None:
        return "ocr"
    i = PIPELINE_STEPS.index(last)
    return PIPELINE_STEPS[min(i + 1, len(PIPELINE_STEPS) - 1)]


@router.post(
    "/v1/episodes/{episode_id}/process",
    status_code=202,
    response_model=ProcessResponse,
    responses={429: {"description": "QUOTA_EXCEEDED"}},
)
def start_process(
    episode_id: str,
    request: Request,
    body: ProcessRequest | None = None,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    body = body or ProcessRequest()
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    episode, series = episode_for_author(db, episode_id, user, lock=True)
    if episode.state not in ("uploaded", "failed"):
        raise state_invalid("현재 상태에서는 분석을 시작할 수 없습니다.", state=episode.state)
    ensure_no_active_job(db, episode.id)
    if episode.image_count == 0:
        raise state_invalid("업로드된 이미지가 없습니다.", state=episode.state)

    resumable = _resume_step(episode.pipeline_last_step)
    step = body.from_step or resumable
    if PIPELINE_STEPS.index(step) > PIPELINE_STEPS.index(resumable):
        raise ApiError(
            400,
            "VALIDATION_ERROR",
            "이전 단계가 끝나지 않아 이 단계부터 시작할 수 없습니다.",
            {"from_step": step, "resumable_step": resumable},
        )
    if step == "ocr":
        if body.from_step == "ocr":
            # 처음부터 다시: 저장된 OCR 결과를 버린다.
            db.execute(
                update(EpisodeImage).where(EpisodeImage.episode_id == episode.id).values(ocr_result=None)
            )
        pending = db.scalar(
            select(func.count())
            .select_from(EpisodeImage)
            .where(
                EpisodeImage.episode_id == episode.id,
                EpisodeImage.upload_status == "uploaded",
                EpisodeImage.ocr_result.is_(None),
            )
        )
        quota.ensure_available(db, series.author_id, quota.OCR_IMAGES, int(pending or 0))

    episode.pipeline_last_step = PIPELINE_STEPS[PIPELINE_STEPS.index(step) - 1] if step != "ocr" else None
    episode.process_options = body.options.model_dump()
    episode.state = "analyzing" if step in ("ocr", "structure") else "synthesizing"
    episode.updated_at = utcnow()
    job = create_job(db, episode, kind="process", step=step, payload={"from_step": step}, user=user)
    content = {
        "job": {"id": job.id, "kind": job.kind, "status": job.status, "step": job.step},
        "episode": {"state": episode.state},
    }
    return idem.commit(202, content)


@router.get("/v1/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)) -> dict:
    job, _ = job_for_author(db, job_id, user)
    return job_view(job)


@router.get("/v1/episodes/{episode_id}/jobs", response_model=Page[JobOut])
def list_jobs(
    episode_id: str,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user)
    rows, next_cursor = paginate(
        db,
        select(Job).where(Job.episode_id == episode.id),
        [Job.created_at, Job.id],
        lambda j: (j.created_at, j.id),
        limit=check_limit(limit),
        cursor=cursor,
        descending=True,
    )
    return {"items": [job_view(j) for j in rows], "next_cursor": next_cursor}


@router.post("/v1/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)) -> dict:
    job, _ = job_for_author(db, job_id, user)
    episode, _ = episode_for_author(db, job.episode_id, user, lock=True)
    db.refresh(job, with_for_update=True)
    if job.status not in ("queued", "running"):
        raise ApiError(409, "JOB_NOT_CANCELABLE", "이미 끝난 작업입니다.", {"status": job.status})
    request_cancel(db, job, episode)
    db.commit()
    return job_view(job)
