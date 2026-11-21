"""회차 상태 규칙, 문제 장면 판정(flags), 집계 컬럼 갱신."""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import job_in_progress, state_invalid
from app.models import AudioAsset, Character, DialogueLine, Episode, EpisodeImage, Job
from app.models.job import ACTIVE_JOB_STATUSES
from app.models.studio import VOICE_KINDS

EDITABLE_STATES = ("needs_review", "scheduled", "published")
PROCESSING_STATES = ("analyzing", "synthesizing")
# 이미지 추가 업로드가 가능한 상태 (분석 전)
UPLOADABLE_STATES = ("draft", "uploaded", "failed")
# 이미지 삭제 가능 상태: 분석 전 + 검수 중 (api.md §4.4)
IMAGE_DELETABLE_STATES = ("draft", "uploaded", "failed", "needs_review")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_editable(episode: Episode) -> None:
    if episode.state not in EDITABLE_STATES:
        raise state_invalid("현재 상태에서는 수정할 수 없습니다.", state=episode.state)


def active_job(db: Session, episode_id: str) -> Job | None:
    return db.scalar(select(Job).where(Job.episode_id == episode_id, Job.status.in_(ACTIVE_JOB_STATUSES)))


def ensure_no_active_job(db: Session, episode_id: str) -> None:
    job = active_job(db, episode_id)
    if job is not None:
        raise job_in_progress(job.id)


def mark_changed(episode: Episode) -> None:
    """게시(또는 예약)된 회차를 수정하면 독자 스냅샷과 달라졌음을 표시한다."""
    episode.updated_at = utcnow()
    if episode.state in ("published", "scheduled"):
        episode.has_unpublished_changes = True


def line_flags(line: DialogueLine, image_reviewed: bool, threshold: float | None = None) -> list[str]:
    """§6.2 flags. low_ocr_confidence·speaker_unknown은 이미지가 검수 완료되면 제외(§4.6 집계 규칙)."""
    if threshold is None:
        threshold = get_settings().low_ocr_confidence_threshold
    flags: list[str] = []
    if line.kind != "ignore" and not image_reviewed:
        if line.ocr_confidence is not None and line.ocr_confidence < threshold:
            flags.append("low_ocr_confidence")
        if line.kind == "dialogue" and line.character_id is None:
            flags.append("speaker_unknown")
    if line.audio_status == "failed":
        flags.append("audio_failed")
    if line.audio_status == "stale":
        flags.append("audio_stale")
    return flags


def recompute_stats(db: Session, episode: Episode) -> None:
    """회차 집계 컬럼 갱신. 대사·이미지·작업이 바뀐 트랜잭션 안에서 호출한다."""
    db.flush()
    images = db.execute(
        select(EpisodeImage.id, EpisodeImage.reviewed).where(
            EpisodeImage.episode_id == episode.id, EpisodeImage.upload_status == "uploaded"
        )
    ).all()
    reviewed = {row.id: row.reviewed for row in images}
    episode.image_count = len(images)
    episode.reviewed_image_count = sum(1 for r in reviewed.values() if r)

    threshold = get_settings().low_ocr_confidence_threshold
    rows = db.execute(
        select(DialogueLine, AudioAsset.duration_ms)
        .outerjoin(AudioAsset, AudioAsset.id == DialogueLine.audio_asset_id)
        .where(DialogueLine.episode_id == episode.id)
    ).all()
    counts = {"low_ocr_confidence": 0, "speaker_unknown": 0, "audio_failed": 0, "audio_stale": 0}
    issues = 0
    duration = 0
    has_audio = False
    for line, asset_duration in rows:
        flags = line_flags(line, reviewed.get(line.image_id, False), threshold)
        if flags:
            issues += 1
        for flag in flags:
            counts[flag] += 1
        if line.kind in VOICE_KINDS and asset_duration is not None:
            has_audio = True
            duration += asset_duration
    episode.issue_count = issues
    episode.issue_low_ocr_confidence = counts["low_ocr_confidence"]
    episode.issue_speaker_unknown = counts["speaker_unknown"]
    episode.issue_audio_failed = counts["audio_failed"]
    episode.issue_audio_stale = counts["audio_stale"]
    episode.audio_duration_ms = duration if has_audio else None


def reindex_images(db: Session, episode_id: str) -> None:
    images = db.scalars(
        select(EpisodeImage)
        .where(EpisodeImage.episode_id == episode_id, EpisodeImage.upload_status == "uploaded")
        .order_by(EpisodeImage.position, EpisodeImage.created_at)
    ).all()
    for i, image in enumerate(images):
        image.position = i


def next_episode_number(db: Session, series_id: str) -> int:
    current = db.scalar(select(func.max(Episode.number)).where(Episode.series_id == series_id))
    return (current or 0) + 1


def narration_character(db: Session, series_id: str) -> Character | None:
    return db.scalar(
        select(Character)
        .where(Character.series_id == series_id, Character.kind == "narration")
        .order_by(Character.created_at)
        .limit(1)
    )


def mark_lines_stale(lines: Iterable[DialogueLine]) -> list[str]:
    """음성 대상 대사를 재생성 필요(stale)로 표시. 음원 연결은 유지(재생은 가능, 게시는 불가)."""
    changed = []
    for line in lines:
        if line.kind in VOICE_KINDS and line.audio_status != "stale":
            line.audio_status = "stale"
            changed.append(line.id)
    return changed
