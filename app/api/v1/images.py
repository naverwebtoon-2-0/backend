import logging
import os

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_author
from app.errors import not_found, state_invalid, validation_error
from app.ids import new_id
from app.integrations import get_integrations
from app.models import EpisodeImage, User
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.episodes import (
    ImageIds,
    ImageOut,
    ImagesCompleteResponse,
    ImagesPresignRequest,
    ImagesPresignResponse,
    ImageUpdate,
)
from app.services.access import episode_for_author
from app.services.episodes import (
    IMAGE_DELETABLE_STATES,
    UPLOADABLE_STATES,
    ensure_editable,
    ensure_no_active_job,
    mark_changed,
    recompute_stats,
    reindex_images,
    utcnow,
)
from app.services.uploads import validate_file_spec, verify_uploaded_image
from app.services.views import image_view

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/episodes/{episode_id}/images", tags=["images"], responses=ERROR_RESPONSES)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _uploaded_images(db: Session, episode_id: str) -> list[EpisodeImage]:
    return list(
        db.scalars(
            select(EpisodeImage)
            .where(EpisodeImage.episode_id == episode_id, EpisodeImage.upload_status == "uploaded")
            .order_by(EpisodeImage.position)
        )
    )


def _image(db: Session, episode_id: str, image_id: str) -> EpisodeImage:
    image = db.get(EpisodeImage, image_id)
    if image is None or image.episode_id != episode_id or image.upload_status != "uploaded":
        raise not_found("이미지를 찾을 수 없습니다.")
    return image


@router.post("/presign", response_model=ImagesPresignResponse)
def presign_images(
    episode_id: str,
    body: ImagesPresignRequest,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    if episode.state not in UPLOADABLE_STATES:
        raise state_invalid("분석을 시작한 회차에는 이미지를 추가할 수 없습니다.", state=episode.state)
    ensure_no_active_job(db, episode.id)
    for i, spec in enumerate(body.files):
        validate_file_spec(spec.content_type, spec.size, index=i, filename=spec.filename)
    existing = len(_uploaded_images(db, episode.id))
    limit = get_settings().max_images_per_episode
    if existing + len(body.files) > limit:
        raise validation_error(
            f"회차당 이미지는 최대 {limit}장입니다.",
            limit=limit,
            existing=existing,
            requested=len(body.files),
        )
    ttl = get_settings().upload_url_ttl_seconds
    storage = get_integrations().storage
    uploads = []
    for i, spec in enumerate(body.files):
        image_id = new_id()
        safe_name = os.path.basename(spec.filename)[:255] or "image"
        key = f"episodes/{episode.id}/images/{image_id}.{_EXT[spec.content_type]}"
        db.add(
            EpisodeImage(
                id=image_id,
                episode_id=episode.id,
                position=existing + i,
                upload_status="pending",
                filename=safe_name,
                content_type=spec.content_type,
                size=spec.size,
                object_key=key,
            )
        )
        uploads.append(
            {
                "image_id": image_id,
                "index": existing + i,
                "method": "PUT",
                "url": storage.presign_put(key, spec.content_type, ttl),
                "headers": {"Content-Type": spec.content_type},
                "expires_in": ttl,
            }
        )
    db.commit()
    return {"uploads": uploads}


@router.post("/complete", response_model=ImagesCompleteResponse)
def complete_images(
    episode_id: str, body: ImageIds, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    if episode.state not in UPLOADABLE_STATES:
        raise state_invalid("분석을 시작한 회차에는 이미지를 추가할 수 없습니다.", state=episode.state)
    ensure_no_active_job(db, episode.id)
    ids = list(dict.fromkeys(body.image_ids))
    images = {
        img.id: img
        for img in db.scalars(
            select(EpisodeImage).where(EpisodeImage.id.in_(ids), EpisodeImage.episode_id == episode.id)
        )
    }
    missing = [i for i in ids if i not in images]
    if missing:
        raise validation_error("이 회차에 발급되지 않은 image_id가 있습니다.", image_ids=missing)
    pending = [images[i] for i in ids if images[i].upload_status == "pending"]
    limit = get_settings().max_images_per_episode
    if len(_uploaded_images(db, episode.id)) + len(pending) > limit:
        raise validation_error(f"회차당 이미지는 최대 {limit}장입니다.", limit=limit)
    for image in pending:
        width, height = verify_uploaded_image(
            image.object_key, image.content_type, image.size, image_id=image.id
        )
        image.width, image.height = width, height
        image.upload_status = "uploaded"
    if pending:
        db.flush()
        reindex_images(db, episode.id)
        if episode.state in ("draft", "failed"):
            episode.state = "uploaded"
        # 이미지가 바뀌었으므로 이전 파이프라인 진행분은 이어서 쓰지 않는다.
        episode.pipeline_last_step = None
        episode.updated_at = utcnow()
        recompute_stats(db, episode)
    db.commit()
    return {"episode": {"id": episode.id, "state": episode.state, "image_count": episode.image_count}}


@router.get("", response_model=Page[ImageOut])
def list_images(episode_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)) -> dict:
    episode, _ = episode_for_author(db, episode_id, user)
    return {"items": [image_view(img) for img in _uploaded_images(db, episode.id)], "next_cursor": None}


@router.put("/order", response_model=Page[ImageOut])
def reorder_images(
    episode_id: str, body: ImageIds, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    if episode.state in ("analyzing", "synthesizing"):
        raise state_invalid("처리 중에는 순서를 바꿀 수 없습니다.", state=episode.state)
    ensure_no_active_job(db, episode.id)
    images = _uploaded_images(db, episode.id)
    if len(body.image_ids) != len(images) or set(body.image_ids) != {img.id for img in images}:
        raise validation_error("image_ids는 이 회차의 모든 이미지를 한 번씩 포함해야 합니다.")
    by_id = {img.id: img for img in images}
    for position, image_id in enumerate(body.image_ids):
        by_id[image_id].position = position
    mark_changed(episode)
    db.commit()
    return {"items": [image_view(by_id[i]) for i in body.image_ids], "next_cursor": None}


@router.patch("/{image_id}", response_model=ImageOut)
def update_image(
    episode_id: str,
    image_id: str,
    body: ImageUpdate,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
) -> dict:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    image = _image(db, episode.id, image_id)
    ensure_editable(episode)
    if body.reviewed is not None and body.reviewed != image.reviewed:
        image.reviewed = body.reviewed
        image.reviewed_at = utcnow() if body.reviewed else None
    if body.alt is not None:
        new_alt = body.alt.strip() or None
        if new_alt != image.alt:
            image.alt = new_alt
            mark_changed(episode)
    recompute_stats(db, episode)
    db.commit()
    return image_view(image)


@router.delete("/{image_id}", status_code=204)
def delete_image(
    episode_id: str, image_id: str, user: User = Depends(require_author), db: Session = Depends(get_db)
) -> Response:
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    image = _image(db, episode.id, image_id)
    if episode.state not in IMAGE_DELETABLE_STATES:
        raise state_invalid("현재 상태에서는 이미지를 삭제할 수 없습니다.", state=episode.state)
    ensure_no_active_job(db, episode.id)
    key = image.object_key
    db.delete(image)  # 대사·효과음 큐·cues는 FK CASCADE로 함께 삭제
    db.flush()
    reindex_images(db, episode.id)
    recompute_stats(db, episode)
    if episode.state in ("uploaded", "failed"):
        episode.pipeline_last_step = None
        if episode.image_count == 0:
            episode.state = "draft"
    episode.updated_at = utcnow()
    db.commit()
    try:
        get_integrations().storage.delete(key)
    except Exception:  # 저장소 정리는 실패해도 응답에 영향 없음
        logger.warning("storage delete failed", extra={"image_id": image_id})
    return Response(status_code=204)
