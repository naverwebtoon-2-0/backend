"""불투명 커서 기반 키셋 페이지네이션. 정렬 키는 (값, id) 튜플이며 방향은 하나로 통일한다."""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Select, tuple_
from sqlalchemy.orm import Session

from app.errors import validation_error

T = TypeVar("T")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _enc(value: Any) -> Any:
    return {"dt": value.isoformat()} if isinstance(value, datetime) else value


def _dec(value: Any) -> Any:
    return datetime.fromisoformat(value["dt"]) if isinstance(value, dict) and "dt" in value else value


def encode_cursor(values: Sequence[Any]) -> str:
    raw = json.dumps([_enc(v) for v in values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, size: int) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise validation_error("cursor 값이 올바르지 않습니다.", field="cursor") from exc
    if not isinstance(values, list) or len(values) != size:
        raise validation_error("cursor 값이 올바르지 않습니다.", field="cursor")
    return [_dec(v) for v in values]


def check_limit(limit: int | None, default: int = DEFAULT_LIMIT) -> int:
    if limit is None:
        return default
    if limit < 1 or limit > MAX_LIMIT:
        raise validation_error(f"limit은 1~{MAX_LIMIT} 사이여야 합니다.", field="limit")
    return limit


def paginate(
    db: Session,
    stmt: Select,
    columns: Sequence[Any],
    key: Callable[[Any], Sequence[Any]],
    *,
    limit: int,
    cursor: str | None,
    descending: bool = False,
    scalars: bool = True,
) -> tuple[list[Any], str | None]:
    if cursor:
        values = decode_cursor(cursor, len(columns))
        cond = tuple_(*columns) < tuple_(*values) if descending else tuple_(*columns) > tuple_(*values)
        stmt = stmt.where(cond)
    stmt = stmt.order_by(*[c.desc() if descending else c.asc() for c in columns]).limit(limit + 1)
    result = db.execute(stmt)
    rows = list(result.scalars() if scalars else result.all())
    next_cursor = encode_cursor(key(rows[limit - 1])) if len(rows) > limit else None
    return rows[:limit], next_cursor
