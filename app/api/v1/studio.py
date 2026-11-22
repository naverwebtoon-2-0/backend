"""작가 스튜디오: 일괄 조회, 대사(lines), 효과음 큐(sfx-cues), 미리듣기 (§6)."""

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_author
from app.errors import ApiError, not_found, validation_error
from app.idempotency import Idempotency
from app.integrations import get_integrations
from app.integrations.http import UpstreamError
from app.models import (
    AudioAsset,
    Character,
    Cue,
    DialogueLine,
    Episode,
    EpisodeImage,
    SfxCue,
    User,
)
from app.models.studio import VOICE_KINDS
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.studio import (
    LineCreate,
    LineOrder,
    LineOut,
    SfxCueCreate,
    SfxCueOut,
    SfxCueUpdate,
    StudioResponse,
)
from app.schemas.studio import LineUpdate as LineUpdateBody
from app.services.access import episode_for_author, line_for_author, sfx_cue_for_author
from app.services.audio import sfx_asset
from app.services.episodes import ensure_editable, mark_changed, recompute_stats
from app.services.views import (
    character_view,
    image_view,
    load_line_views,
    load_sfx_views,
    signed_get,
)

router = APIRouter(tags=["studio"], responses=ERROR_RESPONSES)


def _ordered_lines(db: Session, episode_id: str, image_id: str | None = None) -> list[DialogueLine]:
    stmt = (
        select(DialogueLine)
        .join(EpisodeImage, EpisodeImage.id == DialogueLine.image_id)
        .where(DialogueLine.episode_id == episode_id)
        .order_by(EpisodeImage.position, DialogueLine.y_ratio, DialogueLine.sort_order, DialogueLine.id)
    )
    if image_id:
        stmt = stmt.where(DialogueLine.image_id == image_id)
    return list(db.scalars(stmt))


def _episode_image(db: Session, episode: Episode, image_id: str) -> EpisodeImage:
    image = db.get(EpisodeImage, image_id)
    if image is None or image.episode_id != episode.id or image.upload_status != "uploaded":
        raise validation_error("이 회차의 이미지가 아닙니다.", field="image_id")
    return image


def _series_character(db: Session, episode: Episode, character_id: str) -> Character:
    character = db.get(Character, character_id)
    if character is None or character.series_id != episode.series_id:
        raise validation_error("이 작품의 인물이 아닙니다.", field="character_id")
    return character


def _sfx_tags() -> set[str]:
    return {p["tag"] for p in get_integrations().dubbing.list_sfx_presets()}


def _upstream(exc: UpstreamError) -> ApiError:
    return ApiError(502, "AI_UPSTREAM_ERROR", "CLOVA 호출에 실패했습니다.", {"service": exc.service})


# ── 스튜디오 일괄 조회 ─────────────────────────────────


@router.get("/v1/episodes/{episode_id}/studio", response_model=StudioResponse)
def get_studio(
    episode_id: str,
    image_id: str | None = Query(default=None),
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    episode, series = episode_for_author(db, episode_id, user)
    images = list(
        db.scalars(
            select(EpisodeImage)
            .where(EpisodeImage.episode_id == episode.id, EpisodeImage.upload_status == "uploaded")
            .order_by(EpisodeImage.position)
        )
    )
    if image_id is not None and image_id not in {img.id for img in images}:
        raise not_found("이미지를 찾을 수 없습니다.")
    characters = db.scalars(
        select(Character).where(Character.series_id == series.id).order_by(Character.created_at)
    ).all()
    cue_stmt = select(SfxCue).where(SfxCue.episode_id == episode.id).order_by(SfxCue.y_ratio, SfxCue.id)
    if image_id:
        cue_stmt = cue_stmt.where(SfxCue.image_id == image_id)
    return {
        "episode": {
            "id": episode.id,
            "state": episode.state,
            "has_unpublished_changes": episode.has_unpublished_changes,
        },
        "images": [image_view(img) for img in images],
        "characters": [character_view(c) for c in characters],
        "lines": load_line_views(db, _ordered_lines(db, episode.id, image_id)),
        "sfx_cues": load_sfx_views(db, list(db.scalars(cue_stmt))),
    }


# ── 대사 ──────────────────────────────────────────────


@router.get("/v1/episodes/{episode_id}/lines", response_model=Page[LineOut])
def list_lines(
    episode_id: str,
    image_id: str | None = Query(default=None),
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user)
    return {"items": load_line_views(db, _ordered_lines(db, episode.id, image_id)), "next_cursor": None}


@router.post("/v1/episodes/{episode_id}/lines", status_code=201, response_model=LineOut)
def create_line(
    episode_id: str,
    body: LineCreate,
    request: Request,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    ensure_editable(episode)
    _episode_image(db, episode, body.image_id)
    if body.character_id:
        _series_character(db, episode, body.character_id)
    bbox = body.bbox.model_dump() if body.bbox else None
    y_ratio = body.y_ratio if body.y_ratio is not None else (bbox["y"] if bbox else 0.0)
    order = body.order
    if order is None:
        order = (
            db.scalar(
                select(func.max(DialogueLine.sort_order)).where(
                    DialogueLine.image_id == body.image_id, DialogueLine.y_ratio == y_ratio
                )
            )
            or 0
        ) + 1
    line = DialogueLine(
        episode_id=episode.id,
        image_id=body.image_id,
        sort_order=order,
        text=body.text,
        bbox=bbox,
        y_ratio=y_ratio,
        kind=body.kind,
        character_id=body.character_id,
        emotion=body.emotion,
        audio_status="stale" if body.kind in VOICE_KINDS else None,
    )
    db.add(line)
    mark_changed(episode)
    recompute_stats(db, episode)
    return idem.commit(201, load_line_views(db, [line])[0])


def apply_line_patch(db: Session, episode: Episode, line: DialogueLine, body: LineUpdateBody) -> None:
    """대사 수정과 stale 규칙(§6.2). 텍스트·화자·감정·종류가 바뀌면 재생성 필요."""
    fields = body.model_fields_set
    for key in ("text", "emotion", "kind"):
        if key in fields and getattr(body, key) is None:
            raise validation_error(f"{key}에 null을 보낼 수 없습니다.", field=key)
    if "character_id" in fields and body.character_id is not None:
        _series_character(db, episode, body.character_id)

    old_kind = line.kind
    audio_changed = False
    if "text" in fields and body.text != line.text:
        line.text = body.text
        audio_changed = True
    if "character_id" in fields and body.character_id != line.character_id:
        line.character_id = body.character_id
        audio_changed = True
    if "emotion" in fields and body.emotion != line.emotion:
        line.emotion = body.emotion
        audio_changed = True
    if "kind" in fields and body.kind != line.kind:
        line.kind = body.kind
        audio_changed = True
    if "bbox" in fields:
        new_bbox = body.bbox.model_dump() if body.bbox else None
        old_y = (line.bbox or {}).get("y")
        if "y_ratio" not in fields and new_bbox and old_y is not None and line.y_ratio == old_y:
            line.y_ratio = new_bbox["y"]  # 큐 위치를 따로 바꾼 적이 없으면 말풍선을 따라간다
        line.bbox = new_bbox
    if "y_ratio" in fields:
        if body.y_ratio is None:
            raise validation_error("y_ratio에 null을 보낼 수 없습니다.", field="y_ratio")
        line.y_ratio = body.y_ratio

    if line.kind not in VOICE_KINDS:
        # 음성화하지 않는 종류로 바꾸면 기존 음원 연결을 해제한다.
        line.audio_status = None
        line.audio_asset_id = None
        line.audio_input_hash = None
    elif audio_changed or old_kind not in VOICE_KINDS:
        line.audio_status = "stale"


@router.patch("/v1/lines/{line_id}", response_model=LineOut)
def update_line(
    line_id: str, body: LineUpdateBody, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    line, episode = line_for_author(db, line_id, user)
    ensure_editable(episode)
    apply_line_patch(db, episode, line, body)
    mark_changed(episode)
    recompute_stats(db, episode)
    db.commit()
    return load_line_views(db, [line])[0]


@router.delete("/v1/lines/{line_id}", status_code=204)
def delete_line(
    line_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> Response:
    line, episode = line_for_author(db, line_id, user)
    ensure_editable(episode)
    db.execute(delete(Cue).where(Cue.id == line.id))
    db.delete(line)
    mark_changed(episode)
    recompute_stats(db, episode)
    db.commit()
    return Response(status_code=204)


@router.put("/v1/episodes/{episode_id}/lines/order", response_model=Page[LineOut])
def reorder_lines(
    episode_id: str, body: LineOrder, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    ensure_editable(episode)
    if len(set(body.line_ids)) != len(body.line_ids):
        raise validation_error("line_ids에 중복이 있습니다.")
    lines = {
        line.id: line
        for line in db.scalars(
            select(DialogueLine).where(
                DialogueLine.id.in_(body.line_ids), DialogueLine.episode_id == episode.id
            )
        )
    }
    missing = [i for i in body.line_ids if i not in lines]
    if missing:
        raise validation_error("이 회차의 대사가 아닌 id가 있습니다.", line_ids=missing)
    # order는 y_ratio가 같은 대사끼리의 순서만 정한다 (§6.2).
    for position, line_id in enumerate(body.line_ids, start=1):
        lines[line_id].sort_order = position
    mark_changed(episode)
    db.commit()
    return {"items": load_line_views(db, [lines[i] for i in body.line_ids]), "next_cursor": None}


@router.get("/v1/lines/{line_id}/audio", status_code=302, response_class=RedirectResponse)
def line_audio(line_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)):
    line = db.get(DialogueLine, line_id)
    if line is None:
        raise not_found("대사를 찾을 수 없습니다.")
    episode_for_author(db, line.episode_id, user)
    asset = db.get(AudioAsset, line.audio_asset_id) if line.audio_asset_id else None
    if asset is None:
        raise not_found("음원이 없습니다.")
    return RedirectResponse(
        signed_get(asset.object_key), status_code=302, headers={"Cache-Control": "no-store"}
    )


# ── 효과음 큐 ─────────────────────────────────────────


@router.post("/v1/episodes/{episode_id}/sfx-cues", status_code=201, response_model=SfxCueOut)
def create_sfx_cue(
    episode_id: str,
    body: SfxCueCreate,
    request: Request,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    ensure_editable(episode)
    _episode_image(db, episode, body.image_id)
    if body.sfx_tag not in _sfx_tags():
        raise validation_error("알 수 없는 효과음 프리셋입니다.", field="sfx_tag")
    try:
        asset = sfx_asset(db, get_integrations(), body.sfx_tag)
    except UpstreamError as exc:
        raise _upstream(exc) from exc
    cue = SfxCue(
        episode_id=episode.id,
        image_id=body.image_id,
        y_ratio=body.y_ratio,
        sfx_tag=body.sfx_tag,
        volume=body.volume,
        asset_id=asset.id,
        duration_ms=asset.duration_ms,
    )
    db.add(cue)
    mark_changed(episode)
    db.flush()
    return idem.commit(201, load_sfx_views(db, [cue])[0])


@router.patch("/v1/sfx-cues/{cue_id}", response_model=SfxCueOut)
def update_sfx_cue(
    cue_id: str, body: SfxCueUpdate, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    cue, episode = sfx_cue_for_author(db, cue_id, user)
    ensure_editable(episode)
    fields = body.model_dump(exclude_unset=True)
    for key, value in fields.items():
        if value is None:
            raise validation_error(f"{key}에 null을 보낼 수 없습니다.", field=key)
    if "image_id" in fields:
        _episode_image(db, episode, fields["image_id"])
        cue.image_id = fields["image_id"]
    if "y_ratio" in fields:
        cue.y_ratio = fields["y_ratio"]
    if "volume" in fields:
        cue.volume = fields["volume"]  # 볼륨만 바꾸면 음원을 다시 만들지 않는다 (재생 시 적용)
    if "sfx_tag" in fields and fields["sfx_tag"] != cue.sfx_tag:
        if fields["sfx_tag"] not in _sfx_tags():
            raise validation_error("알 수 없는 효과음 프리셋입니다.", field="sfx_tag")
        try:
            asset = sfx_asset(db, get_integrations(), fields["sfx_tag"])
        except UpstreamError as exc:
            raise _upstream(exc) from exc
        cue.sfx_tag, cue.asset_id, cue.duration_ms = fields["sfx_tag"], asset.id, asset.duration_ms
    if fields:
        mark_changed(episode)
    db.commit()
    return load_sfx_views(db, [cue])[0]


@router.delete("/v1/sfx-cues/{cue_id}", status_code=204)
def delete_sfx_cue(
    cue_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> Response:
    cue, episode = sfx_cue_for_author(db, cue_id, user)
    ensure_editable(episode)
    db.execute(delete(Cue).where(Cue.id == cue.id))
    db.delete(cue)
    mark_changed(episode)
    db.commit()
    return Response(status_code=204)
