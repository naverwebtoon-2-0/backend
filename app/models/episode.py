from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, TimestampMixin, check_in

EPISODE_STATES = (
    "draft",
    "uploaded",
    "analyzing",
    "synthesizing",
    "needs_review",
    "scheduled",
    "published",
    "failed",
)
PIPELINE_STEPS = ("ocr", "structure", "dubbing", "build_cues")
UPLOAD_STATUSES = ("pending", "uploaded")


class Episode(IdMixin, TimestampMixin, Base):
    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint(check_in("state", EPISODE_STATES), name="ck_episodes_state"),
        CheckConstraint(
            "pipeline_last_step IS NULL OR " + check_in("pipeline_last_step", PIPELINE_STEPS),
            name="ck_episodes_pipeline_last_step",
        ),
        Index("ix_episodes_series_id_number", "series_id", "number"),
        Index("ix_episodes_state_scheduled_at", "state", "scheduled_at"),
    )

    series_id: Mapped[str] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    has_unpublished_changes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 파이프라인 재개용: 마지막으로 성공한 단계와 시작 옵션
    pipeline_last_step: Mapped[str | None] = mapped_column(String(16))
    process_options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # 게시 스냅샷 버전: live = 독자에게 보이는 버전, pending = 예약 게시 대기 버전
    live_playback_version: Mapped[int | None] = mapped_column(Integer)
    pending_playback_version: Mapped[int | None] = mapped_column(Integer)

    # 집계 컬럼 (§4.6 구현 메모: 변경 시 갱신, 조회 시 재계산하지 않음)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewed_image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issue_low_ocr_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issue_speaker_unknown: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issue_audio_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issue_audio_stale: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audio_duration_ms: Mapped[int | None] = mapped_column(BigInteger)


class EpisodeImage(IdMixin, CreatedMixin, Base):
    __tablename__ = "episode_images"
    __table_args__ = (
        CheckConstraint(check_in("upload_status", UPLOAD_STATUSES), name="ck_episode_images_upload_status"),
        Index("ix_episode_images_episode_id_position", "episode_id", "position"),
    )

    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    alt: Mapped[str | None] = mapped_column(String(500))
    reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ocr_result: Mapped[list | None] = mapped_column(JSONB)


class EpisodePlayback(IdMixin, CreatedMixin, Base):
    __tablename__ = "episode_playbacks"
    __table_args__ = (UniqueConstraint("episode_id", "version", name="uq_episode_playbacks_version"),)

    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
