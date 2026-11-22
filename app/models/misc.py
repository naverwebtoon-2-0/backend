from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import CreatedMixin, IdMixin


class ReaderSettings(Base):
    __tablename__ = "reader_settings"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReadingProgress(Base):
    __tablename__ = "reading_progress"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True)
    image_index: Mapped[int] = mapped_column(Integer, nullable=False)
    y_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IdempotencyKey(IdMixin, CreatedMixin, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)


class UsageCounter(Base):
    __tablename__ = "usage_counters"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM (UTC)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
