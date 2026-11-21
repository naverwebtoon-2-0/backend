from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, check_in

JOB_KINDS = ("process", "regenerate")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "canceled")
JOB_STEPS = ("ocr", "structure", "dubbing", "build_cues", "done")
ACTIVE_JOB_STATUSES = ("queued", "running")


class Job(IdMixin, CreatedMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(check_in("kind", JOB_KINDS), name="ck_jobs_kind"),
        CheckConstraint(check_in("status", JOB_STATUSES), name="ck_jobs_status"),
        CheckConstraint(check_in("step", JOB_STEPS), name="ck_jobs_step"),
        # 회차당 진행 중(queued/running) 작업은 하나만
        Index(
            "uq_jobs_one_active_per_episode",
            "episode_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_jobs_status_created_at", "status", "created_at"),
        Index("ix_jobs_episode_id_created_at", "episode_id", "created_at"),
    )

    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    step: Mapped[str] = mapped_column(String(16), nullable=False)
    progress_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[dict | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ETA 계산용: 단계 시작 시각과 단계별 항목당 평균 처리 시간(초)
    step_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    step_rates: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
