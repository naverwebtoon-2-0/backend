from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_author
from app.errors import validation_error
from app.idempotency import Idempotency
from app.ids import new_id
from app.integrations import get_integrations
from app.integrations.dubbing import DEFAULT_NARRATION_VOICE
from app.models import Character, Episode, Job, Series, User
from app.pagination import check_limit, paginate
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.series import (
    CoverPresignRequest,
    CoverPresignResponse,
    SeriesCreate,
    SeriesOut,
    SeriesUpdate,
)
from app.services.access import series_for_author
from app.services.uploads import validate_file_spec, verify_uploaded_image
from app.services.views import episode_counts, series_view

router = APIRouter(prefix="/v1/series", tags=["series"], responses=ERROR_RESPONSES)


@router.post("", status_code=201, response_model=SeriesOut)
def create_series(
    body: SeriesCreate, request: Request, user: User = Depends(require_author), db: Session = Depends(get_db)
):
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    series = Series(author_id=user.id, **body.model_dump())
    db.add(series)
    db.flush()
    # 작품마다 기본 나레이션 인물을 둔다 (narration·description 기본 화자, §6.2)
    db.add(
        Character(series_id=series.id, name="나레이션", kind="narration", voice_id=DEFAULT_NARRATION_VOICE)
    )
    db.flush()
    db.refresh(series)
    return idem.commit(201, series_view(series, 0))


@router.get("", response_model=Page[SeriesOut])
def list_series(
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    rows, next_cursor = paginate(
        db,
        select(Series).where(Series.author_id == user.id),
        [Series.created_at, Series.id],
        lambda s: (s.created_at, s.id),
        limit=check_limit(limit),
        cursor=cursor,
        descending=True,
    )
    counts = episode_counts(db, [s.id for s in rows])
    return {"items": [series_view(s, counts.get(s.id, 0)) for s in rows], "next_cursor": next_cursor}


@router.get("/{series_id}", response_model=SeriesOut)
def get_series(series_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)) -> dict:
    series = series_for_author(db, series_id, user)
    return series_view(series, episode_counts(db, [series.id]).get(series.id, 0))


@router.patch("/{series_id}", response_model=SeriesOut)
def update_series(
    series_id: str, body: SeriesUpdate, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    series = series_for_author(db, series_id, user, lock=True)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None:
            raise validation_error(f"{key}에 null을 보낼 수 없습니다.", field=key)
        setattr(series, key, value)
    db.commit()
    db.refresh(series)
    return series_view(series, episode_counts(db, [series.id]).get(series.id, 0))


@router.delete("/{series_id}", status_code=204)
def delete_series(
    series_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> Response:
    series = series_for_author(db, series_id, user, lock=True)
    running = db.scalar(
        select(Job.id)
        .join(Episode, Episode.id == Job.episode_id)
        .where(Episode.series_id == series.id, Job.status.in_(("queued", "running")))
        .limit(1)
    )
    if running is not None:
        from app.errors import job_in_progress

        raise job_in_progress(running)
    db.delete(series)
    db.commit()
    return Response(status_code=204)


@router.post("/{series_id}/cover/presign", response_model=CoverPresignResponse)
def presign_cover(
    series_id: str,
    body: CoverPresignRequest,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    series = series_for_author(db, series_id, user, lock=True)
    validate_file_spec(body.content_type, body.size)
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[body.content_type]
    key = f"covers/{series.id}/{new_id()}.{ext}"
    series.pending_cover_object_key = key
    series.pending_cover_content_type = body.content_type
    series.pending_cover_size = body.size
    db.commit()
    ttl = get_settings().upload_url_ttl_seconds
    url = get_integrations().storage.presign_put(key, body.content_type, ttl)
    return {
        "upload": {
            "method": "PUT",
            "url": url,
            "headers": {"Content-Type": body.content_type},
            "expires_in": ttl,
        }
    }


@router.post("/{series_id}/cover/complete", response_model=SeriesOut)
def complete_cover(
    series_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    series = series_for_author(db, series_id, user, lock=True)
    if not series.pending_cover_object_key:
        raise validation_error("발급된 표지 업로드가 없습니다.")
    verify_uploaded_image(
        series.pending_cover_object_key,
        series.pending_cover_content_type or "",
        series.pending_cover_size or 0,
    )
    series.cover_object_key = series.pending_cover_object_key
    series.pending_cover_object_key = None
    series.pending_cover_content_type = None
    series.pending_cover_size = None
    db.commit()
    db.refresh(series)
    return series_view(series, episode_counts(db, [series.id]).get(series.id, 0))
