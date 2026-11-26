from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, check_in

LINE_KINDS = ("dialogue", "narration", "description", "sfx_text", "ignore")
VOICE_KINDS = ("dialogue", "narration", "description")
EMOTIONS = ("neutral", "happy", "sad", "angry", "scared", "surprised", "confused", "whisper", "shout")
AUDIO_STATUSES = ("ready", "stale", "generating", "failed")
CUE_KINDS = ("dialogue", "narration", "description", "sfx")


class DialogueLine(IdMixin, TimestampMixin, Base):
    __tablename__ = "dialogue_lines"
    __table_args__ = (
        CheckConstraint(check_in("kind", LINE_KINDS), name="ck_dialogue_lines_kind"),
        CheckConstraint(check_in("emotion", EMOTIONS), name="ck_dialogue_lines_emotion"),
        CheckConstraint(
            "audio_status IS NULL OR " + check_in("audio_status", AUDIO_STATUSES),
            name="ck_dialogue_lines_audio_status",
        ),
        CheckConstraint("y_ratio >= 0 AND y_ratio <= 1", name="ck_dialogue_lines_y_ratio"),
        Index("ix_dialogue_lines_episode_id", "episode_id"),
        Index("ix_dialogue_lines_image_order", "image_id", "y_ratio", "sort_order"),
        Index("ix_dialogue_lines_character_id", "character_id"),
    )

    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    image_id: Mapped[str] = mapped_column(ForeignKey("episode_images.id", ondelete="CASCADE"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    original_text: Mapped[str | None] = mapped_column(Text)
    bbox: Mapped[dict | None] = mapped_column(JSONB)
    y_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    character_id: Mapped[str | None] = mapped_column(ForeignKey("characters.id", ondelete="SET NULL"))
    emotion: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    audio_status: Mapped[str | None] = mapped_column(String(16))
    audio_error: Mapped[dict | None] = mapped_column(JSONB)
    audio_asset_id: Mapped[str | None] = mapped_column(ForeignKey("audio_assets.id", ondelete="SET NULL"))
    # 현재 연결된 음원을 만든 입력의 해시 (작업 중 수정 감지용)
    audio_input_hash: Mapped[str | None] = mapped_column(String(64))


class SfxCue(IdMixin, TimestampMixin, Base):
    __tablename__ = "sfx_cues"
    __table_args__ = (
        CheckConstraint("y_ratio >= 0 AND y_ratio <= 1", name="ck_sfx_cues_y_ratio"),
        CheckConstraint("volume >= 0 AND volume <= 1", name="ck_sfx_cues_volume"),
        Index("ix_sfx_cues_episode_id", "episode_id"),
        Index("ix_sfx_cues_image_id_y_ratio", "image_id", "y_ratio"),
    )

    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    image_id: Mapped[str] = mapped_column(ForeignKey("episode_images.id", ondelete="CASCADE"), nullable=False)
    y_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    sfx_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("audio_assets.id", ondelete="SET NULL"))
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class Cue(IdMixin, Base):
    """재생 타임라인(build_cues 결과). id는 원본 대사·효과음 큐 id와 같다."""

    __tablename__ = "cues"
    __table_args__ = (
        CheckConstraint(check_in("kind", CUE_KINDS), name="ck_cues_kind"),
        Index("ix_cues_episode_order", "episode_id", "image_index", "y_ratio", "sort_order"),
    )

    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    image_id: Mapped[str] = mapped_column(ForeignKey("episode_images.id", ondelete="CASCADE"), nullable=False)
    image_index: Mapped[int] = mapped_column(Integer, nullable=False)
    y_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("audio_assets.id", ondelete="SET NULL"))
    text: Mapped[str | None] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(String(100))
    label: Mapped[str | None] = mapped_column(String(200))
    volume: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
