"""소유권 검사(IDOR 방지). 없으면 404, 남의 리소스면 403 (api.md §1)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import forbidden, not_found
from app.models import Character, DialogueLine, Episode, Job, Series, SfxCue, User


def series_for_author(db: Session, series_id: str, user: User, *, lock: bool = False) -> Series:
    stmt = select(Series).where(Series.id == series_id)
    if lock:
        stmt = stmt.with_for_update()
    series = db.scalar(stmt)
    if series is None:
        raise not_found("작품을 찾을 수 없습니다.")
    if series.author_id != user.id:
        raise forbidden("본인 작품만 사용할 수 있습니다.")
    return series


def episode_for_author(
    db: Session, episode_id: str, user: User, *, lock: bool = False
) -> tuple[Episode, Series]:
    stmt = select(Episode).where(Episode.id == episode_id)
    if lock:
        stmt = stmt.with_for_update()
    episode = db.scalar(stmt)
    if episode is None:
        raise not_found("회차를 찾을 수 없습니다.")
    series = db.get(Series, episode.series_id)
    if series is None or series.author_id != user.id:
        raise forbidden("본인 작품만 사용할 수 있습니다.")
    return episode, series


def line_for_author(db: Session, line_id: str, user: User) -> tuple[DialogueLine, Episode]:
    line = db.get(DialogueLine, line_id)
    if line is None:
        raise not_found("대사를 찾을 수 없습니다.")
    episode, _ = episode_for_author(db, line.episode_id, user, lock=True)
    return line, episode


def sfx_cue_for_author(db: Session, cue_id: str, user: User) -> tuple[SfxCue, Episode]:
    cue = db.get(SfxCue, cue_id)
    if cue is None:
        raise not_found("효과음 큐를 찾을 수 없습니다.")
    episode, _ = episode_for_author(db, cue.episode_id, user, lock=True)
    return cue, episode


def character_for_author(db: Session, character_id: str, user: User) -> tuple[Character, Series]:
    character = db.get(Character, character_id)
    if character is None:
        raise not_found("인물을 찾을 수 없습니다.")
    return character, series_for_author(db, character.series_id, user)


def job_for_author(db: Session, job_id: str, user: User) -> tuple[Job, Episode]:
    job = db.get(Job, job_id)
    if job is None:
        raise not_found("작업을 찾을 수 없습니다.")
    episode, _ = episode_for_author(db, job.episode_id, user)
    return job, episode
