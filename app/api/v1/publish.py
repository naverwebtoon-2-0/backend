"""게시·게시 취소(§8), 게시 전 미리보기(§6.6)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_author
from app.idempotency import Idempotency
from app.models import User
from app.schemas.common import ERROR_RESPONSES
from app.schemas.episodes import EpisodeOut, PublishRequest
from app.schemas.public import PublicEpisodeOut
from app.services import publishing
from app.services.access import episode_for_author
from app.services.playback import build_snapshot, playback_view
from app.services.views import episode_view

router = APIRouter(tags=["publish"], responses=ERROR_RESPONSES)


@router.post("/v1/episodes/{episode_id}/publish", response_model=EpisodeOut)
def publish_episode(
    episode_id: str,
    request: Request,
    body: PublishRequest | None = None,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    body = body or PublishRequest()
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    publishing.publish(db, episode, body.publish_at, body.allow_unreviewed)
    return idem.commit(200, episode_view(episode))


@router.post("/v1/episodes/{episode_id}/unpublish", response_model=EpisodeOut)
def unpublish_episode(
    episode_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    publishing.unpublish(db, episode)
    db.commit()
    return episode_view(episode)


@router.get("/v1/episodes/{episode_id}/preview", response_model=PublicEpisodeOut)
def preview_episode(
    episode_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    """독자 뷰어와 같은 형식의 타임라인. 게시 스냅샷이 아니라 현재 편집 상태로 만든다."""
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    data = build_snapshot(db, episode)
    db.commit()
    images, playback = playback_view(data, episode.live_playback_version or 0)
    return {
        "id": episode.id,
        "title": episode.title,
        "number": episode.number,
        "series_id": episode.series_id,
        "language": data["language"],
        "images": images,
        "playback": playback,
        "prev_episode_id": None,
        "next_episode_id": None,
    }
