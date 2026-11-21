"""DB 모델 → 응답 dict. 서명 URL은 여기서 발급한다."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import get_integrations
from app.models import (
    AudioAsset,
    Character,
    DialogueLine,
    Episode,
    EpisodeImage,
    Job,
    Series,
    SfxCue,
    User,
)
from app.services.episodes import line_flags, utcnow


def signed_get(key: str | None, ttl: int | None = None) -> str | None:
    if not key:
        return None
    return get_integrations().storage.presign_get(key, ttl or get_settings().image_url_ttl_seconds)


def user_view(user: User) -> dict:
    return {"id": user.id, "email": user.email, "nickname": user.nickname, "role": user.role}


def series_view(series: Series, episode_count: int) -> dict:
    return {
        "id": series.id,
        "title": series.title,
        "description": series.description,
        "language": series.language,
        "genre": series.genre,
        "serial_status": series.serial_status,
        "cover_url": signed_get(series.cover_object_key),
        "episode_count": episode_count,
        "created_at": series.created_at,
        "updated_at": series.updated_at,
    }


def episode_counts(db: Session, series_ids: list[str], *, published_only: bool = False) -> dict[str, int]:
    if not series_ids:
        return {}
    stmt = (
        select(Episode.series_id, func.count())
        .where(Episode.series_id.in_(series_ids))
        .group_by(Episode.series_id)
    )
    if published_only:
        stmt = stmt.where(Episode.state == "published")
    return {row[0]: row[1] for row in db.execute(stmt).all()}


def eta_seconds(job: Job, now: datetime | None = None) -> int | None:
    """남은 시간 예상: 현재 단계 처리 속도(없으면 직전 단계 평균 속도) × 남은 양."""
    if job.status != "running" or job.progress_total <= 0:
        return None
    remaining = max(0, job.progress_total - job.progress_done)
    now = now or utcnow()
    rate: float | None = None
    if job.progress_done > 0 and job.step_started_at is not None:
        rate = max(0.0, (now - job.step_started_at).total_seconds()) / job.progress_done
    elif job.step_rates:
        rate = float(list(job.step_rates.values())[-1])
    if rate is None:
        return None
    return round(rate * remaining)


def job_view(job: Job) -> dict:
    return {
        "id": job.id,
        "episode_id": job.episode_id,
        "kind": job.kind,
        "status": job.status,
        "step": job.step,
        "progress": {"done": job.progress_done, "total": job.progress_total},
        "eta_seconds": eta_seconds(job),
        "error": job.error,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def processing_view(job: Job | None) -> dict | None:
    if job is None:
        return None
    return {
        "job_id": job.id,
        "kind": job.kind,
        "step": job.step,
        "progress": {"done": job.progress_done, "total": job.progress_total},
        "eta_seconds": eta_seconds(job),
    }


def stats_view(episode: Episode, job: Job | None) -> dict:
    progress = episode.reviewed_image_count / episode.image_count if episode.image_count else 0.0
    return {
        "reviewed_image_count": episode.reviewed_image_count,
        "review_progress": round(progress, 4),
        "issue_count": episode.issue_count,
        "issue_breakdown": {
            "low_ocr_confidence": episode.issue_low_ocr_confidence,
            "speaker_unknown": episode.issue_speaker_unknown,
            "audio_failed": episode.issue_audio_failed,
            "audio_stale": episode.issue_audio_stale,
        },
        "audio_duration_ms": episode.audio_duration_ms,
        "processing": processing_view(job),
    }


def episode_view(episode: Episode, *, stats: bool = False, job: Job | None = None) -> dict:
    data = {
        "id": episode.id,
        "series_id": episode.series_id,
        "number": episode.number,
        "title": episode.title,
        "state": episode.state,
        "image_count": episode.image_count,
        "published_at": episode.published_at,
        "scheduled_at": episode.scheduled_at,
        "has_unpublished_changes": episode.has_unpublished_changes,
        "playback_version": episode.live_playback_version,
        "created_at": episode.created_at,
        "updated_at": episode.updated_at,
    }
    if stats:
        data["stats"] = stats_view(episode, job)
    return data


def active_jobs_by_episode(db: Session, episode_ids: list[str]) -> dict[str, Job]:
    if not episode_ids:
        return {}
    jobs = db.scalars(
        select(Job).where(Job.episode_id.in_(episode_ids), Job.status.in_(("queued", "running")))
    ).all()
    return {job.episode_id: job for job in jobs}


def image_view(image: EpisodeImage) -> dict:
    return {
        "id": image.id,
        "index": image.position,
        "url": signed_get(image.object_key),
        "width": image.width,
        "height": image.height,
        "alt": image.alt,
        "reviewed": image.reviewed,
        "reviewed_at": image.reviewed_at,
    }


def character_view(character: Character) -> dict:
    return {
        "id": character.id,
        "series_id": character.series_id,
        "name": character.name,
        "kind": character.kind,
        "voice_id": character.voice_id,
        "voice_params": character.voice_params or {},
    }


def audio_view(line: DialogueLine, asset: AudioAsset | None) -> dict | None:
    if line.audio_status is None:
        return None
    return {
        "asset_id": asset.id if asset else None,
        "url": signed_get(asset.object_key) if asset else None,
        "duration_ms": asset.duration_ms if asset else None,
        "status": line.audio_status,
    }


def line_view(line: DialogueLine, asset: AudioAsset | None, image_reviewed: bool) -> dict:
    return {
        "id": line.id,
        "image_id": line.image_id,
        "order": line.sort_order,
        "text": line.text,
        "original_text": line.original_text,
        "bbox": line.bbox,
        "y_ratio": line.y_ratio,
        "kind": line.kind,
        "character_id": line.character_id,
        "emotion": line.emotion,
        "ocr_confidence": line.ocr_confidence,
        "audio": audio_view(line, asset),
        "flags": line_flags(line, image_reviewed),
    }


def sfx_cue_view(cue: SfxCue, asset: AudioAsset | None) -> dict:
    return {
        "id": cue.id,
        "image_id": cue.image_id,
        "y_ratio": cue.y_ratio,
        "sfx_tag": cue.sfx_tag,
        "asset_id": cue.asset_id,
        "url": signed_get(asset.object_key) if asset else None,
        "volume": cue.volume,
        "duration_ms": cue.duration_ms,
    }


def load_line_views(db: Session, lines: list[DialogueLine]) -> list[dict]:
    """대사 목록을 음원·이미지 검수 상태와 함께 직렬화한다 (N+1 방지)."""
    asset_ids = {line.audio_asset_id for line in lines if line.audio_asset_id}
    assets = (
        {a.id: a for a in db.scalars(select(AudioAsset).where(AudioAsset.id.in_(asset_ids)))}
        if asset_ids
        else {}
    )
    image_ids = {line.image_id for line in lines}
    reviewed = (
        {
            row[0]: row[1]
            for row in db.execute(
                select(EpisodeImage.id, EpisodeImage.reviewed).where(EpisodeImage.id.in_(image_ids))
            ).all()
        }
        if image_ids
        else {}
    )
    return [
        line_view(line, assets.get(line.audio_asset_id or ""), reviewed.get(line.image_id, False))
        for line in lines
    ]


def load_sfx_views(db: Session, cues: list[SfxCue]) -> list[dict]:
    asset_ids = {c.asset_id for c in cues if c.asset_id}
    assets = (
        {a.id: a for a in db.scalars(select(AudioAsset).where(AudioAsset.id.in_(asset_ids)))}
        if asset_ids
        else {}
    )
    return [sfx_cue_view(c, assets.get(c.asset_id or "")) for c in cues]
