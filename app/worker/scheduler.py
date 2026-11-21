"""예약 게시 스케줄러: scheduled_at <= now() 인 회차를 게시한다 (워커가 30초 주기로 호출)."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Episode
from app.services.episodes import utcnow
from app.services.publishing import activate_pending_playback

logger = logging.getLogger(__name__)


def publish_due_episodes(s: Session) -> list[str]:
    published: list[str] = []
    due = list(
        s.scalars(select(Episode.id).where(Episode.state == "scheduled", Episode.scheduled_at <= utcnow()))
    )
    for episode_id in due:
        try:
            episode = s.scalar(
                select(Episode).where(Episode.id == episode_id).with_for_update(skip_locked=True)
            )
            if episode is None or episode.state != "scheduled":
                s.rollback()
                continue
            activate_pending_playback(s, episode)
            s.commit()
            published.append(episode_id)
            logger.info("scheduled episode published", extra={"episode_id": episode_id})
        except Exception:
            # 실패해도 다음 주기에 다시 시도한다.
            s.rollback()
            logger.exception("scheduled publish failed", extra={"episode_id": episode_id})
    return published
