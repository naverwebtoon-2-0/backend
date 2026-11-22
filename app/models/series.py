from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, check_in

GENRES = (
    "romance",
    "fantasy",
    "action",
    "drama",
    "comedy",
    "thriller",
    "daily",
    "sports",
    "historical",
    "etc",
)
SERIAL_STATUSES = ("ongoing", "completed", "hiatus")
CHARACTER_KINDS = ("character", "narration")


class Series(IdMixin, TimestampMixin, Base):
    __tablename__ = "series"
    __table_args__ = (
        CheckConstraint(check_in("genre", GENRES), name="ck_series_genre"),
        CheckConstraint(check_in("serial_status", SERIAL_STATUSES), name="ck_series_serial_status"),
        Index("ix_series_author_id", "author_id"),
    )

    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ko")
    genre: Mapped[str] = mapped_column(String(32), nullable=False)
    serial_status: Mapped[str] = mapped_column(String(16), nullable=False, default="ongoing")
    cover_object_key: Mapped[str | None] = mapped_column(String(512))
    pending_cover_object_key: Mapped[str | None] = mapped_column(String(512))
    pending_cover_content_type: Mapped[str | None] = mapped_column(String(64))
    pending_cover_size: Mapped[int | None] = mapped_column()


class Character(IdMixin, TimestampMixin, Base):
    __tablename__ = "characters"
    __table_args__ = (
        CheckConstraint(check_in("kind", CHARACTER_KINDS), name="ck_characters_kind"),
        Index("ix_characters_series_id", "series_id"),
    )

    series_id: Mapped[str] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="character")
    voice_id: Mapped[str | None] = mapped_column(String(100))
    voice_params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
