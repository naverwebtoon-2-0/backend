from sqlalchemy import BigInteger, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, check_in

AUDIO_KINDS = ("voice", "sfx")


class AudioAsset(IdMixin, CreatedMixin, Base):
    __tablename__ = "audio_assets"
    __table_args__ = (CheckConstraint(check_in("kind", AUDIO_KINDS), name="ck_audio_assets_kind"),)

    hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="audio/wav")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
