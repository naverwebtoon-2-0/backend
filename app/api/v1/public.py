"""독자용 공개 API (§9.1~9.3). 비로그인 허용, 게시 스냅샷만 노출한다."""

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import not_found, validation_error
from app.models import Episode, EpisodePlayback, Series, User
from app.models.series import GENRES, SERIAL_STATUSES
from app.pagination import check_limit, paginate
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.public import PublicEpisodeOut, PublicEpisodeSummary, PublicSeriesOut, TranscriptOut
from app.services.playback import playback_view, transcript_items
from app.services.views import episode_counts, signed_get

router = APIRouter(prefix="/v1/public", tags=["public"], responses=ERROR_RESPONSES)
_PRIVATE = {"Cache-Control": "private, no-store"}


def _published_exists():
    return exists().where(Episode.series_id == Series.id, Episode.state == "published")


def _public_series(db: Session, series_id: str) -> Series:
    series = db.scalar(select(Series).where(Series.id == series_id, _published_exists()))
    if series is None:
        raise not_found("작품을 찾을 수 없습니다.")
    return series


def _series_view(series: Series, author: User | None, count: int) -> dict:
    return {
        "id": series.id,
        "title": series.title,
        "description": series.description,
        "language": series.language,
        "genre": series.genre,
        "serial_status": series.serial_status,
        "cover_url": signed_get(series.cover_object_key),
        "episode_count": count,
        "author": {"nickname": author.nickname if author else None},
        "created_at": series.created_at,
        "updated_at": series.updated_at,
    }


def _json(content: dict) -> JSONResponse:
    return JSONResponse(jsonable_encoder(content), headers=_PRIVATE)


@router.get("/series", response_model=Page[PublicSeriesOut])
def list_public_series(
    q: str | None = Query(default=None, max_length=100),
    genre: str | None = Query(default=None),
    serial_status: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if genre is not None and genre not in GENRES:
        raise validation_error("알 수 없는 장르입니다.", field="genre")
    if serial_status is not None and serial_status not in SERIAL_STATUSES:
        raise validation_error("알 수 없는 연재 상태입니다.", field="serial_status")
    stmt = select(Series).where(_published_exists())
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Series.title.ilike(f"%{escaped}%", escape="\\"))
    if genre:
        stmt = stmt.where(Series.genre == genre)
    if serial_status:
        stmt = stmt.where(Series.serial_status == serial_status)
    rows, next_cursor = paginate(
        db,
        stmt,
        [Series.created_at, Series.id],
        lambda s: (s.created_at, s.id),
        limit=check_limit(limit),
        cursor=cursor,
        descending=True,
    )
    counts = episode_counts(db, [s.id for s in rows], published_only=True)
    authors = {u.id: u for u in db.scalars(select(User).where(User.id.in_({s.author_id for s in rows})))}
    return _json(
        {
            "items": [_series_view(s, authors.get(s.author_id), counts.get(s.id, 0)) for s in rows],
            "next_cursor": next_cursor,
        }
    )


@router.get("/series/{series_id}", response_model=PublicSeriesOut)
def get_public_series(series_id: str, db: Session = Depends(get_db)):
    series = _public_series(db, series_id)
    count = episode_counts(db, [series.id], published_only=True).get(series.id, 0)
    return _json(_series_view(series, db.get(User, series.author_id), count))


@router.get("/series/{series_id}/episodes", response_model=Page[PublicEpisodeSummary])
def list_public_episodes(
    series_id: str,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    series = _public_series(db, series_id)
    rows, next_cursor = paginate(
        db,
        select(Episode).where(Episode.series_id == series.id, Episode.state == "published"),
        [Episode.number, Episode.id],
        lambda e: (e.number, e.id),
        limit=check_limit(limit, default=50),
        cursor=cursor,
    )
    snapshots = _snapshots(db, rows)
    items = []
    for e in rows:
        data = snapshots.get(e.id, {})
        items.append(
            {
                "id": e.id,
                "series_id": e.series_id,
                "number": e.number,
                "title": e.title,
                "published_at": e.published_at,
                "image_count": len(data.get("images", [])),
                "audio_duration_ms": sum(
                    c.get("duration_ms") or 0 for c in data.get("cues", []) if c["kind"] != "sfx"
                )
                or None,
            }
        )
    return _json({"items": items, "next_cursor": next_cursor})


def _snapshots(db: Session, episodes: list[Episode]) -> dict[str, dict]:
    pairs = [(e.id, e.live_playback_version) for e in episodes if e.live_playback_version]
    if not pairs:
        return {}
    rows = db.scalars(
        select(EpisodePlayback).where(EpisodePlayback.episode_id.in_([p[0] for p in pairs]))
    ).all()
    wanted = dict(pairs)
    return {r.episode_id: r.data for r in rows if wanted.get(r.episode_id) == r.version}


def _published(db: Session, episode_id: str) -> tuple[Episode, EpisodePlayback]:
    episode = db.get(Episode, episode_id)
    if episode is None or episode.state != "published" or episode.live_playback_version is None:
        raise not_found("회차를 찾을 수 없습니다.")
    snapshot = db.scalar(
        select(EpisodePlayback).where(
            EpisodePlayback.episode_id == episode.id, EpisodePlayback.version == episode.live_playback_version
        )
    )
    if snapshot is None:
        raise not_found("회차를 찾을 수 없습니다.")
    return episode, snapshot


def _neighbor(db: Session, episode: Episode, *, before: bool) -> str | None:
    stmt = select(Episode.id).where(Episode.series_id == episode.series_id, Episode.state == "published")
    if before:
        stmt = stmt.where(Episode.number < episode.number).order_by(Episode.number.desc())
    else:
        stmt = stmt.where(Episode.number > episode.number).order_by(Episode.number.asc())
    return db.scalar(stmt.limit(1))


@router.get("/episodes/{episode_id}", response_model=PublicEpisodeOut)
def get_public_episode(episode_id: str, db: Session = Depends(get_db)):
    episode, snapshot = _published(db, episode_id)
    images, playback = playback_view(snapshot.data, snapshot.version)
    return _json(
        {
            "id": episode.id,
            "title": episode.title,
            "number": episode.number,
            "series_id": episode.series_id,
            "language": snapshot.data.get("language", "ko"),
            "images": images,
            "playback": playback,
            "prev_episode_id": _neighbor(db, episode, before=True),
            "next_episode_id": _neighbor(db, episode, before=False),
        }
    )


@router.get("/episodes/{episode_id}/cues/{cue_id}/audio", status_code=302, response_class=RedirectResponse)
def public_cue_audio(episode_id: str, cue_id: str, db: Session = Depends(get_db)):
    _, snapshot = _published(db, episode_id)
    cue = next((c for c in snapshot.data.get("cues", []) if c["id"] == cue_id), None)
    if cue is None or not cue.get("object_key"):
        raise not_found("음원을 찾을 수 없습니다.")
    url = signed_get(cue["object_key"], get_settings().audio_url_ttl_seconds)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


@router.get("/episodes/{episode_id}/transcript", response_model=TranscriptOut)
def public_transcript(episode_id: str, db: Session = Depends(get_db)):
    episode, snapshot = _published(db, episode_id)
    return _json({"episode_id": episode.id, "items": transcript_items(snapshot.data)})
