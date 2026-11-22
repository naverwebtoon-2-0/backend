import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_author
from app.errors import job_in_progress, state_invalid, validation_error
from app.idempotency import Idempotency
from app.models import Episode, User
from app.pagination import check_limit, paginate
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.episodes import DashboardResponse, EpisodeCreate, EpisodeOut, EpisodeUpdate
from app.services.access import episode_for_author, series_for_author
from app.services.episodes import PROCESSING_STATES, active_job, next_episode_number, utcnow
from app.services.views import active_jobs_by_episode, episode_view, stats_view

router = APIRouter(tags=["episodes"], responses=ERROR_RESPONSES)
SEOUL = ZoneInfo("Asia/Seoul")


def _include_stats(include: str | None) -> bool:
    if include is None:
        return False
    parts = {p.strip() for p in include.split(",") if p.strip()}
    if parts - {"stats"}:
        raise validation_error("include에는 stats만 쓸 수 있습니다.", field="include")
    return "stats" in parts


@router.post("/v1/series/{series_id}/episodes", status_code=201, response_model=EpisodeOut)
def create_episode(
    series_id: str,
    body: EpisodeCreate,
    request: Request,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    series = series_for_author(db, series_id, user, lock=True)  # 번호 채번 직렬화
    episode = Episode(
        series_id=series.id,
        number=body.number or next_episode_number(db, series.id),
        title=body.title,
        state="draft",
    )
    db.add(episode)
    db.flush()
    db.refresh(episode)
    return idem.commit(201, episode_view(episode))


@router.get(
    "/v1/series/{series_id}/episodes", response_model=Page[EpisodeOut], response_model_exclude_unset=True
)
def list_episodes(
    series_id: str,
    include: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    series = series_for_author(db, series_id, user)
    with_stats = _include_stats(include)
    rows, next_cursor = paginate(
        db,
        select(Episode).where(Episode.series_id == series.id),
        [Episode.number, Episode.id],
        lambda e: (e.number, e.id),
        limit=check_limit(limit, default=50),
        cursor=cursor,
    )
    jobs = active_jobs_by_episode(db, [e.id for e in rows]) if with_stats else {}
    return {
        "items": [episode_view(e, stats=with_stats, job=jobs.get(e.id)) for e in rows],
        "next_cursor": next_cursor,
    }


@router.get("/v1/episodes/{episode_id}", response_model=EpisodeOut, response_model_exclude_unset=True)
def get_episode(
    episode_id: str,
    include: str | None = Query(default=None),
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user)
    with_stats = _include_stats(include)
    return episode_view(episode, stats=with_stats, job=active_job(db, episode.id) if with_stats else None)


@router.patch("/v1/episodes/{episode_id}", response_model=EpisodeOut)
def update_episode(
    episode_id: str, body: EpisodeUpdate, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            raise validation_error(f"{key}에 null을 보낼 수 없습니다.", field=key)
        setattr(episode, key, value)
    episode.updated_at = utcnow()
    db.commit()
    return episode_view(episode)


@router.delete("/v1/episodes/{episode_id}", status_code=204)
def delete_episode(
    episode_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> Response:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    # 게시 전만 삭제 (한 번이라도 게시된 회차는 published_at이 남아 있으므로 삭제하지 않는다)
    if episode.state in ("scheduled", "published") or episode.published_at is not None:
        raise state_invalid("게시된 회차는 삭제할 수 없습니다.", state=episode.state)
    job = active_job(db, episode.id)
    if job is not None or episode.state in PROCESSING_STATES:
        raise job_in_progress(job.id if job else None)
    db.delete(episode)
    db.commit()
    return Response(status_code=204)


_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month_range(month: str | None) -> tuple[str, datetime, datetime]:
    if month is None:
        month = datetime.now(SEOUL).strftime("%Y-%m")
    elif not _MONTH.match(month):
        raise validation_error("month는 YYYY-MM 형식이어야 합니다.", field="month")
    year, mon = map(int, month.split("-"))
    start = datetime(year, mon, 1, tzinfo=SEOUL)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=SEOUL)
    return month, start, end


@router.get("/v1/series/{series_id}/dashboard", response_model=DashboardResponse)
def dashboard(
    series_id: str,
    month: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    series = series_for_author(db, series_id, user)
    month, start, end = _month_range(month)
    summary_row = db.execute(
        select(
            func.count(),
            func.count().filter(Episode.state == "published"),
            func.count().filter(Episode.published_at >= start, Episode.published_at < end),
            func.coalesce(func.sum(Episode.issue_count), 0),
            func.count().filter(Episode.state.in_(PROCESSING_STATES)),
        ).where(Episode.series_id == series.id)
    ).one()
    rows, next_cursor = paginate(
        db,
        select(Episode).where(Episode.series_id == series.id),
        [Episode.number, Episode.id],
        lambda e: (e.number, e.id),
        limit=check_limit(limit, default=50),
        cursor=cursor,
        descending=True,
    )
    jobs = active_jobs_by_episode(db, [e.id for e in rows])
    return {
        "series": {
            "id": series.id,
            "title": series.title,
            "genre": series.genre,
            "serial_status": series.serial_status,
        },
        "summary": {
            "month": month,
            "timezone": "Asia/Seoul",
            "episode_count": summary_row[0],
            "published_count": summary_row[1],
            "published_this_month": summary_row[2],
            "issue_count": int(summary_row[3]),
            "in_progress_count": summary_row[4],
        },
        "episodes": [
            {
                "id": e.id,
                "number": e.number,
                "title": e.title,
                "state": e.state,
                "image_count": e.image_count,
                "published_at": e.published_at,
                "scheduled_at": e.scheduled_at,
                "has_unpublished_changes": e.has_unpublished_changes,
                "updated_at": e.updated_at,
                "stats": stats_view(e, jobs.get(e.id)),
            }
            for e in rows
        ],
        "next_cursor": next_cursor,
    }
