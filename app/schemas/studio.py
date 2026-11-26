from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ErrorBody, StrictModel
from app.schemas.episodes import ImageOut, JobBrief

LineKind = Literal["dialogue", "narration", "description", "sfx_text", "ignore"]
Emotion = Literal["neutral", "happy", "sad", "angry", "scared", "surprised", "confused", "whisper", "shout"]


class BBox(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=0, le=1)
    h: float = Field(ge=0, le=1)


class AudioOut(BaseModel):
    asset_id: str | None
    url: str | None
    duration_ms: int | None
    status: str
    error: ErrorBody | None = None


class LineOut(BaseModel):
    id: str
    image_id: str
    order: int
    text: str
    original_text: str | None
    bbox: dict | None
    y_ratio: float
    kind: str
    character_id: str | None
    emotion: str
    ocr_confidence: float | None
    audio: AudioOut | None
    flags: list[str]


class LineCreate(StrictModel):
    image_id: str
    text: str = Field(min_length=1, max_length=2000)
    kind: LineKind = "dialogue"
    character_id: str | None = None
    emotion: Emotion = "neutral"
    bbox: BBox | None = None
    y_ratio: float | None = Field(default=None, ge=0, le=1)
    order: int | None = Field(default=None, ge=1)


class LineUpdate(StrictModel):
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    character_id: str | None = None
    emotion: Emotion | None = None
    kind: LineKind | None = None
    bbox: BBox | None = None
    y_ratio: float | None = Field(default=None, ge=0, le=1)


class LineOrder(StrictModel):
    line_ids: list[str] = Field(min_length=1)


class SfxCueOut(BaseModel):
    id: str
    image_id: str
    y_ratio: float
    sfx_tag: str
    asset_id: str | None
    url: str | None
    volume: float
    duration_ms: int | None


class SfxCueCreate(StrictModel):
    image_id: str
    y_ratio: float = Field(ge=0, le=1)
    sfx_tag: str = Field(min_length=1, max_length=64)
    volume: float = Field(default=1.0, ge=0, le=1)


class SfxCueUpdate(StrictModel):
    image_id: str | None = None
    y_ratio: float | None = Field(default=None, ge=0, le=1)
    sfx_tag: str | None = Field(default=None, min_length=1, max_length=64)
    volume: float | None = Field(default=None, ge=0, le=1)


class StudioCharacter(BaseModel):
    id: str
    name: str
    kind: str
    voice_id: str | None
    voice_params: dict


class StudioEpisode(BaseModel):
    id: str
    state: str
    has_unpublished_changes: bool


class StudioResponse(BaseModel):
    episode: StudioEpisode
    images: list[ImageOut]
    characters: list[StudioCharacter]
    lines: list[LineOut]
    sfx_cues: list[SfxCueOut]


class Voice(BaseModel):
    id: str
    name: str
    gender: str | None = None
    age: str | None = None
    sample_url: str | None = None


class SfxPreset(BaseModel):
    tag: str
    name: str
    category: str | None = None
    sample_url: str | None = None


class RegenerateRequest(StrictModel):
    scope: Literal["stale", "lines", "all"]
    line_ids: list[str] | None = None


class LineRegenerateRequest(StrictModel):
    force: bool = False


class RegenerateResponse(BaseModel):
    job: JobBrief


class LineAudioResponse(BaseModel):
    audio: AudioOut
