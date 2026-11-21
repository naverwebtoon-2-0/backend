"""응답 JSON 인코딩: 시각은 ISO 8601 UTC(`...Z`)로 통일한다 (api.md §1)."""

from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def encode(content: Any) -> Any:
    return jsonable_encoder(content, custom_encoder={datetime: iso_utc})
