"""AI 사용 쿼터 (월 단위, UTC). 사용량은 작품 소유 작가에게 집계한다."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import quota_exceeded
from app.models import UsageCounter
from app.services.episodes import utcnow

DUBBING_CHARS = "dubbing_chars"
OCR_IMAGES = "ocr_images"


def current_period() -> str:
    return utcnow().strftime("%Y-%m")


def limit_of(metric: str) -> int:
    settings = get_settings()
    return {DUBBING_CHARS: settings.quota_dubbing_chars, OCR_IMAGES: settings.quota_ocr_images}[metric]


def used(db: Session, user_id: str, metric: str) -> int:
    value = db.scalar(
        select(UsageCounter.used).where(
            UsageCounter.user_id == user_id,
            UsageCounter.period == current_period(),
            UsageCounter.metric == metric,
        )
    )
    return int(value or 0)


def ensure_available(db: Session, user_id: str, metric: str, amount: int) -> None:
    current, limit = used(db, user_id, metric), limit_of(metric)
    if amount > 0 and current + amount > limit:
        raise quota_exceeded(metric, current, limit, amount)


def remaining(db: Session, user_id: str, metric: str) -> int:
    return max(0, limit_of(metric) - used(db, user_id, metric))


def consume(db: Session, user_id: str, metric: str, amount: int) -> None:
    if amount <= 0:
        return
    stmt = insert(UsageCounter).values(user_id=user_id, period=current_period(), metric=metric, used=amount)
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageCounter.user_id, UsageCounter.period, UsageCounter.metric],
        set_={"used": UsageCounter.used + amount},
    )
    db.execute(stmt)


def quota_view(db: Session, user_id: str) -> dict:
    return {
        "period": current_period(),
        DUBBING_CHARS: {"used": used(db, user_id, DUBBING_CHARS), "limit": limit_of(DUBBING_CHARS)},
        OCR_IMAGES: {"used": used(db, user_id, OCR_IMAGES), "limit": limit_of(OCR_IMAGES)},
    }
