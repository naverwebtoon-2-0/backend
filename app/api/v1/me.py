from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.errors import not_found
from app.models import Episode, ReaderSettings, ReadingProgress, User
from app.schemas.auth import MeResponse
from app.schemas.common import ERROR_RESPONSES
from app.schemas.public import (
    ProgressIn,
    ProgressOut,
    QuotaOut,
    ReaderSettingsData,
    SettingsOut,
    SettingsPatch,
)
from app.services.public import is_publicly_visible
from app.services.quota import quota_view
from app.services.views import user_view

router = APIRouter(prefix="/v1/me", tags=["me"], responses=ERROR_RESPONSES)


def _reader_settings(db: Session, user: User) -> dict:
    row = db.get(ReaderSettings, user.id)
    return ReaderSettingsData(**(row.data if row else {})).model_dump()


@router.get("", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return {**user_view(user), "settings": {"reader": _reader_settings(db, user)}}


@router.get("/settings", response_model=SettingsOut)
def get_settings_(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return {"reader": _reader_settings(db, user)}


@router.patch("/settings", response_model=SettingsOut)
def patch_settings(
    body: SettingsPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    current = _reader_settings(db, user)
    current.update(body.reader.model_dump(exclude_unset=True, exclude_none=True))
    merged = ReaderSettingsData(**current).model_dump()
    row = db.get(ReaderSettings, user.id)
    if row is None:
        db.add(ReaderSettings(user_id=user.id, data=merged))
    else:
        row.data = merged
    db.commit()
    return {"reader": merged}


@router.get("/quota", response_model=QuotaOut)
def get_quota(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return quota_view(db, user.id)


def _readable_episode(db: Session, episode_id: str, user: User) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None or not is_publicly_visible(db, episode, user):
        raise not_found("회차를 찾을 수 없습니다.")
    return episode


@router.put("/progress/{episode_id}", response_model=ProgressOut)
def put_progress(
    episode_id: str, body: ProgressIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    _readable_episode(db, episode_id, user)
    row = db.get(ReadingProgress, (user.id, episode_id))
    if row is None:
        row = ReadingProgress(user_id=user.id, episode_id=episode_id, image_index=0, y_ratio=0.0)
        db.add(row)
    row.image_index = body.image_index
    row.y_ratio = body.y_ratio
    db.commit()
    db.refresh(row)
    return {
        "episode_id": episode_id,
        "image_index": row.image_index,
        "y_ratio": row.y_ratio,
        "updated_at": row.updated_at,
    }


@router.get("/progress/{episode_id}", response_model=ProgressOut)
def get_progress(
    episode_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user.id, ReadingProgress.episode_id == episode_id
        )
    )
    if row is None:
        raise not_found("저장된 진행 위치가 없습니다.")
    return {
        "episode_id": episode_id,
        "image_index": row.image_index,
        "y_ratio": row.y_ratio,
        "updated_at": row.updated_at,
    }
