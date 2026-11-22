from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import StrictModel


class PublicSeriesOut(BaseModel):
    id: str
    title: str
    description: str
    language: str
    genre: str
    serial_status: str
    cover_url: str | None
    episode_count: int
    author: dict
    created_at: datetime
    updated_at: datetime


class PublicEpisodeSummary(BaseModel):
    id: str
    series_id: str
    number: int
    title: str
    published_at: datetime | None
    image_count: int
    audio_duration_ms: int | None


class PublicImage(BaseModel):
    index: int
    url: str
    width: int | None
    height: int | None
    alt: str


class PublicCue(BaseModel):
    id: str
    image_index: int
    y_ratio: float
    kind: str
    text: str | None = None
    speaker: str | None = None
    label: str | None = None
    audio_url: str | None
    volume: float | None = None
    duration_ms: int | None


class Playback(BaseModel):
    version: int
    urls_expire_at: datetime | None
    cues: list[PublicCue]


class PublicEpisodeOut(BaseModel):
    id: str
    title: str
    number: int
    series_id: str
    language: str
    images: list[PublicImage]
    playback: Playback
    prev_episode_id: str | None
    next_episode_id: str | None


class TranscriptItem(BaseModel):
    index: int
    image_index: int
    kind: str
    speaker: str | None = None
    text: str


class TranscriptOut(BaseModel):
    episode_id: str
    items: list[TranscriptItem]


class ReaderSettingsData(StrictModel):
    autoplay: bool = True
    dialogue_volume: float = Field(default=1.0, ge=0, le=1)
    sfx_volume: float = Field(default=1.0, ge=0, le=1)
    narration: bool = True
    show_subtitles: bool = True
    playback_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    high_contrast: bool = False


class ReaderSettingsPatch(StrictModel):
    autoplay: bool | None = None
    dialogue_volume: float | None = Field(default=None, ge=0, le=1)
    sfx_volume: float | None = Field(default=None, ge=0, le=1)
    narration: bool | None = None
    show_subtitles: bool | None = None
    playback_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    high_contrast: bool | None = None


class SettingsPatch(StrictModel):
    reader: ReaderSettingsPatch


class SettingsOut(BaseModel):
    reader: ReaderSettingsData


class ProgressIn(StrictModel):
    image_index: int = Field(ge=0)
    y_ratio: float = Field(ge=0, le=1)


class ProgressOut(BaseModel):
    episode_id: str
    image_index: int
    y_ratio: float
    updated_at: datetime


class QuotaItem(BaseModel):
    used: int
    limit: int


class QuotaOut(BaseModel):
    period: str
    dubbing_chars: QuotaItem
    ocr_images: QuotaItem
