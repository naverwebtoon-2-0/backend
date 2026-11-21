"""독자 공개 범위 판단."""

from sqlalchemy.orm import Session

from app.models import Episode, User


def is_publicly_visible(db: Session, episode: Episode, user: User | None = None) -> bool:
    """독자는 게시 상태이고 스냅샷이 있는 회차만 본다."""
    return episode.state == "published" and episode.live_playback_version is not None
