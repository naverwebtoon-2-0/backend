from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import StrictModel

Genre = Literal[
    "romance", "fantasy", "action", "drama", "comedy", "thriller", "daily", "sports", "historical", "etc"
]
SerialStatus = Literal["ongoing", "completed", "hiatus"]
ImageType = Literal["image/png", "image/jpeg", "image/webp"]


class SeriesCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    language: str = Field(default="ko", min_length=2, max_length=8)
    genre: Genre
    serial_status: SerialStatus = "ongoing"


class SeriesUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    language: str | None = Field(default=None, min_length=2, max_length=8)
    genre: Genre | None = None
    serial_status: SerialStatus | None = None


class SeriesOut(BaseModel):
    id: str
    title: str
    description: str
    language: str
    genre: str
    serial_status: str
    cover_url: str | None
    episode_count: int
    created_at: datetime
    updated_at: datetime


class FileSpec(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    # 형식·크기 검사는 415/413 오류 코드를 주기 위해 서비스에서 한다.
    content_type: str = Field(max_length=100)
    size: int = Field(gt=0)


class CoverPresignRequest(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(max_length=100)
    size: int = Field(gt=0)


class UploadTicket(BaseModel):
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str]
    expires_in: int


class CoverPresignResponse(BaseModel):
    upload: UploadTicket


class CharacterCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["character", "narration"] = "character"
    voice_id: str | None = Field(default=None, max_length=100)
    voice_params: dict = Field(default_factory=dict)


class CharacterUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    voice_id: str | None = Field(default=None, max_length=100)
    voice_params: dict | None = None


class CharacterOut(BaseModel):
    id: str
    series_id: str
    name: str
    kind: str
    voice_id: str | None
    voice_params: dict


class CharacterMerge(StrictModel):
    from_: str = Field(alias="from")
    into: str
