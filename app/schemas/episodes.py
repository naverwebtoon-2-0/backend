from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import StrictModel
from app.schemas.series import FileSpec


class EpisodeCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    number: int | None = Field(default=None, ge=1)


class EpisodeUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    number: int | None = Field(default=None, ge=1)


class Progress(BaseModel):
    done: int
    total: int


class Processing(BaseModel):
    job_id: str
    kind: str
    step: str
    progress: Progress
    eta_seconds: int | None


class IssueBreakdown(BaseModel):
    low_ocr_confidence: int
    speaker_unknown: int
    audio_failed: int
    audio_stale: int


class EpisodeStats(BaseModel):
    reviewed_image_count: int
    review_progress: float
    issue_count: int
    issue_breakdown: IssueBreakdown
    audio_duration_ms: int | None
    processing: Processing | None


class EpisodeOut(BaseModel):
    id: str
    series_id: str
    number: int
    title: str
    state: str
    image_count: int
    published_at: datetime | None
    scheduled_at: datetime | None
    has_unpublished_changes: bool
    created_at: datetime
    updated_at: datetime
    stats: EpisodeStats | None = None


class ImagesPresignRequest(StrictModel):
    files: list[FileSpec] = Field(min_length=1)


class ImageUploadTicket(BaseModel):
    image_id: str
    index: int
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str]
    expires_in: int


class ImagesPresignResponse(BaseModel):
    uploads: list[ImageUploadTicket]


class ImageIds(StrictModel):
    image_ids: list[str] = Field(min_length=1)


class EpisodeBrief(BaseModel):
    id: str
    state: str
    image_count: int


class ImagesCompleteResponse(BaseModel):
    episode: EpisodeBrief


class ImageOut(BaseModel):
    id: str
    index: int
    url: str
    width: int | None
    height: int | None
    alt: str | None
    reviewed: bool
    reviewed_at: datetime | None


class ImageUpdate(StrictModel):
    reviewed: bool | None = None
    alt: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _at_least_one(self) -> "ImageUpdate":
        if self.reviewed is None and self.alt is None:
            raise ValueError("reviewed 또는 alt 중 하나 이상을 보내야 합니다.")
        return self


class ProcessOptions(StrictModel):
    ocr_language: str = Field(default="ko", min_length=2, max_length=8)
    narration: bool = True
    description: bool = True
    sfx: bool = True


class ProcessRequest(StrictModel):
    from_step: Literal["ocr", "structure", "dubbing", "build_cues"] | None = None
    options: ProcessOptions = Field(default_factory=ProcessOptions)


class JobBrief(BaseModel):
    id: str
    kind: str
    status: str
    step: str


class EpisodeState(BaseModel):
    state: str


class ProcessResponse(BaseModel):
    job: JobBrief
    episode: EpisodeState


class JobError(BaseModel):
    code: str
    message: str
    details: dict | None = None


class JobOut(BaseModel):
    id: str
    episode_id: str
    kind: str
    status: str
    step: str
    progress: Progress
    eta_seconds: int | None
    error: JobError | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DashboardSeries(BaseModel):
    id: str
    title: str
    genre: str
    serial_status: str


class DashboardSummary(BaseModel):
    month: str
    timezone: str
    episode_count: int
    published_count: int
    published_this_month: int
    issue_count: int
    in_progress_count: int


class DashboardEpisode(BaseModel):
    id: str
    number: int
    title: str
    state: str
    image_count: int
    published_at: datetime | None
    scheduled_at: datetime | None
    has_unpublished_changes: bool
    updated_at: datetime
    stats: EpisodeStats


class DashboardResponse(BaseModel):
    series: DashboardSeries
    summary: DashboardSummary
    episodes: list[DashboardEpisode]
    next_cursor: str | None


class PublishRequest(StrictModel):
    publish_at: datetime | None = None
    allow_unreviewed: bool = False
