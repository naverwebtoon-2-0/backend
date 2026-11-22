"""게시·예약 게시·게시 취소 (§8)."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import ApiError, state_invalid
from app.models import DialogueLine, Episode, EpisodeImage, EpisodePlayback
from app.models.studio import VOICE_KINDS
from app.services.episodes import ensure_no_active_job, utcnow
from app.services.playback import build_snapshot


def _save_snapshot(db: Session, episode: Episode) -> int:
    version = (
        db.scalar(select(func.max(EpisodePlayback.version)).where(EpisodePlayback.episode_id == episode.id))
        or 0
    ) + 1
    db.add(EpisodePlayback(episode_id=episode.id, version=version, data=build_snapshot(db, episode)))
    db.flush()
    return version


def check_publishable(db: Session, episode: Episode, allow_unreviewed: bool) -> None:
    pending_lines = list(
        db.scalars(
            select(DialogueLine.id)
            .where(
                DialogueLine.episode_id == episode.id,
                DialogueLine.kind.in_(VOICE_KINDS),
                DialogueLine.audio_status.in_(("stale", "generating")),
            )
            .order_by(DialogueLine.id)
        )
    )
    if pending_lines:
        raise state_invalid(
            "다시 만들어야 하는 음성이 남아 있습니다.", state=episode.state, stale_line_ids=pending_lines
        )
    if not allow_unreviewed:
        unreviewed = list(
            db.scalars(
                select(EpisodeImage.id)
                .where(
                    EpisodeImage.episode_id == episode.id,
                    EpisodeImage.upload_status == "uploaded",
                    EpisodeImage.reviewed.is_(False),
                )
                .order_by(EpisodeImage.position)
            )
        )
        if unreviewed:
            raise ApiError(
                409,
                "REVIEW_INCOMPLETE",
                "검수가 끝나지 않은 이미지가 있습니다.",
                {"unreviewed_image_ids": unreviewed},
            )


def publish(db: Session, episode: Episode, publish_at: datetime | None, allow_unreviewed: bool) -> None:
    if episode.state not in ("needs_review", "published"):
        raise state_invalid("현재 상태에서는 게시할 수 없습니다.", state=episode.state)
    ensure_no_active_job(db, episode.id)
    now = utcnow()
    scheduled = publish_at is not None and publish_at > now
    if scheduled and episode.state == "published":
        # 게시 중인 회차를 예약 상태로 바꾸면 독자가 볼 수 없게 되므로 허용하지 않는다.
        raise state_invalid(
            "게시된 회차는 예약 게시할 수 없습니다. 즉시 재게시하거나 게시를 취소한 뒤 예약하세요.",
            state=episode.state,
        )
    check_publishable(db, episode, allow_unreviewed)
    version = _save_snapshot(db, episode)
    episode.has_unpublished_changes = False
    episode.updated_at = now
    if scheduled:
        episode.state = "scheduled"
        episode.scheduled_at = publish_at
        episode.pending_playback_version = version
    else:
        episode.state = "published"
        episode.live_playback_version = version
        episode.pending_playback_version = None
        episode.scheduled_at = None
        if episode.published_at is None:
            episode.published_at = now


def activate_pending_playback(db: Session, episode: Episode) -> None:
    """예약 시각이 된 회차를 게시한다. published_at은 실제 게시 시각(최초 1회)."""
    version = episode.pending_playback_version or _save_snapshot(db, episode)
    now = utcnow()
    episode.state = "published"
    episode.live_playback_version = version
    episode.pending_playback_version = None
    episode.scheduled_at = None
    episode.updated_at = now
    if episode.published_at is None:
        episode.published_at = now


def unpublish(db: Session, episode: Episode) -> None:
    if episode.state not in ("published", "scheduled"):
        raise state_invalid("게시 또는 예약된 회차가 아닙니다.", state=episode.state)
    episode.state = "needs_review"
    episode.pending_playback_version = None
    episode.scheduled_at = None
    episode.has_unpublished_changes = False
    episode.updated_at = utcnow()
