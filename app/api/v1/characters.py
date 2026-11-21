"""인물(characters): 작품 단위로 회차 간 공유 (§6.3)."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_author
from app.errors import ApiError, validation_error
from app.idempotency import Idempotency
from app.models import Character, DialogueLine, Episode, User
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.series import CharacterCreate, CharacterMerge, CharacterOut, CharacterUpdate
from app.services.access import character_for_author, series_for_author
from app.services.episodes import mark_changed, mark_lines_stale, recompute_stats
from app.services.views import character_view

router = APIRouter(tags=["characters"], responses=ERROR_RESPONSES)


def _stale_lines_of(db: Session, character: Character) -> None:
    """인물 목소리가 바뀌면 그 인물의 모든 대사를 stale로.

    나레이션 인물은 화자 미지정 나레이션·설명문도 포함한다.
    """
    cond = DialogueLine.character_id == character.id
    if character.kind == "narration":
        cond = or_(
            cond,
            (DialogueLine.character_id.is_(None)) & DialogueLine.kind.in_(("narration", "description")),
        )
    lines = list(
        db.scalars(
            select(DialogueLine)
            .join(Episode, Episode.id == DialogueLine.episode_id)
            .where(
                Episode.series_id == character.series_id,
                cond,
                DialogueLine.audio_status.in_(("ready", "failed")),
            )
        )
    )
    mark_lines_stale(lines)
    for episode_id in {line.episode_id for line in lines}:
        episode = db.get(Episode, episode_id)
        mark_changed(episode)
        recompute_stats(db, episode)


@router.get("/v1/series/{series_id}/characters", response_model=Page[CharacterOut])
def list_characters(
    series_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    series = series_for_author(db, series_id, user)
    rows = db.scalars(
        select(Character).where(Character.series_id == series.id).order_by(Character.created_at)
    )
    return {"items": [character_view(c) for c in rows], "next_cursor": None}


@router.post("/v1/series/{series_id}/characters", status_code=201, response_model=CharacterOut)
def create_character(
    series_id: str,
    body: CharacterCreate,
    request: Request,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    series = series_for_author(db, series_id, user)
    character = Character(series_id=series.id, **body.model_dump())
    db.add(character)
    db.flush()
    return idem.commit(201, character_view(character))


@router.patch("/v1/characters/{character_id}", response_model=CharacterOut)
def update_character(
    character_id: str,
    body: CharacterUpdate,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    character, _ = character_for_author(db, character_id, user)
    fields = body.model_fields_set
    if "name" in fields:
        if body.name is None:
            raise validation_error("name에 null을 보낼 수 없습니다.", field="name")
        character.name = body.name
    voice_changed = False
    if "voice_id" in fields and body.voice_id != character.voice_id:
        character.voice_id = body.voice_id
        voice_changed = True
    if "voice_params" in fields:
        params = body.voice_params or {}
        if params != (character.voice_params or {}):
            character.voice_params = params
            voice_changed = True
    if voice_changed:
        _stale_lines_of(db, character)
    db.commit()
    return character_view(character)


@router.delete("/v1/characters/{character_id}", status_code=204)
def delete_character(
    character_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> Response:
    character, _ = character_for_author(db, character_id, user)
    in_use = db.scalar(select(DialogueLine.id).where(DialogueLine.character_id == character.id).limit(1))
    if in_use is not None:
        raise ApiError(409, "CHARACTER_IN_USE", "대사가 있는 인물은 삭제할 수 없습니다. 먼저 병합하세요.")
    db.delete(character)
    db.commit()
    return Response(status_code=204)


@router.post("/v1/characters/merge", response_model=CharacterOut)
def merge_characters(
    body: CharacterMerge, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    if body.from_ == body.into:
        raise validation_error("같은 인물끼리는 병합할 수 없습니다.")
    source, _ = character_for_author(db, body.from_, user)
    target, _ = character_for_author(db, body.into, user)
    if source.series_id != target.series_id:
        raise validation_error("같은 작품의 인물끼리만 병합할 수 있습니다.")
    lines = list(db.scalars(select(DialogueLine).where(DialogueLine.character_id == source.id)))
    same_voice = source.voice_id == target.voice_id and (source.voice_params or {}) == (
        target.voice_params or {}
    )
    for line in lines:
        line.character_id = target.id
    if not same_voice:
        mark_lines_stale([line for line in lines if line.audio_status in ("ready", "failed")])
    db.flush()
    for episode_id in {line.episode_id for line in lines}:
        episode = db.get(Episode, episode_id)
        mark_changed(episode)
        recompute_stats(db, episode)
    db.delete(source)
    db.commit()
    return character_view(target)
