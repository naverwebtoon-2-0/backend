"""재생 타임라인(cues) 생성과 게시 스냅샷.

큐 정렬: (image_index, y_ratio, order). 대사 큐 id = 대사 id, 효과음 큐 id = 효과음 큐 id.
스냅샷에는 음원·이미지의 object_key를 저장하고, 조회할 때마다 서명 URL을 새로 만든다.
"""

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import get_integrations
from app.models import AudioAsset, Character, Cue, DialogueLine, Episode, EpisodeImage, Series, SfxCue
from app.models.studio import VOICE_KINDS
from app.services.episodes import utcnow
from app.services.views import signed_get


def sfx_labels() -> dict[str, str]:
    return {p["tag"]: p["name"] for p in get_integrations().dubbing.list_sfx_presets()}


def rebuild_cues(db: Session, episode: Episode) -> int:
    """현재 대사·효과음 큐로 cues 테이블을 다시 만든다 (build_cues 단계, 미리보기·게시 직전)."""
    db.flush()
    db.execute(delete(Cue).where(Cue.episode_id == episode.id))
    images = {
        img.id: img.position
        for img in db.scalars(
            select(EpisodeImage).where(
                EpisodeImage.episode_id == episode.id, EpisodeImage.upload_status == "uploaded"
            )
        )
    }
    characters = {
        c.id: c for c in db.scalars(select(Character).where(Character.series_id == episode.series_id))
    }
    assets: dict[str, AudioAsset] = {}
    lines = list(
        db.scalars(
            select(DialogueLine).where(
                DialogueLine.episode_id == episode.id, DialogueLine.kind.in_(VOICE_KINDS)
            )
        )
    )
    cues_src = list(db.scalars(select(SfxCue).where(SfxCue.episode_id == episode.id)))
    asset_ids = {line.audio_asset_id for line in lines if line.audio_asset_id} | {
        c.asset_id for c in cues_src if c.asset_id
    }
    if asset_ids:
        assets = {a.id: a for a in db.scalars(select(AudioAsset).where(AudioAsset.id.in_(asset_ids)))}
    labels = sfx_labels()
    count = 0
    for line in lines:
        if line.image_id not in images:
            continue
        character = characters.get(line.character_id) if line.character_id else None
        asset = assets.get(line.audio_asset_id or "")
        db.add(
            Cue(
                id=line.id,
                episode_id=episode.id,
                image_id=line.image_id,
                image_index=images[line.image_id],
                y_ratio=line.y_ratio,
                sort_order=line.sort_order,
                kind=line.kind,
                asset_id=asset.id if asset else None,
                text=line.text,
                speaker=character.name if character else None,
                duration_ms=asset.duration_ms if asset else None,
            )
        )
        count += 1
    for cue in cues_src:
        if cue.image_id not in images:
            continue
        asset = assets.get(cue.asset_id or "")
        db.add(
            Cue(
                id=cue.id,
                episode_id=episode.id,
                image_id=cue.image_id,
                image_index=images[cue.image_id],
                y_ratio=cue.y_ratio,
                sort_order=0,
                kind="sfx",
                asset_id=asset.id if asset else None,
                label=labels.get(cue.sfx_tag, cue.sfx_tag),
                volume=cue.volume,
                duration_ms=cue.duration_ms or (asset.duration_ms if asset else None),
            )
        )
        count += 1
    db.flush()
    return count


def build_snapshot(db: Session, episode: Episode) -> dict:
    """현재 상태로 재생 타임라인을 만들어 스냅샷(JSON)을 돌려준다."""
    rebuild_cues(db, episode)
    series = db.get(Series, episode.series_id)
    images = list(
        db.scalars(
            select(EpisodeImage)
            .where(EpisodeImage.episode_id == episode.id, EpisodeImage.upload_status == "uploaded")
            .order_by(EpisodeImage.position)
        )
    )
    rows = db.execute(
        select(Cue, AudioAsset.object_key)
        .outerjoin(AudioAsset, AudioAsset.id == Cue.asset_id)
        .where(Cue.episode_id == episode.id)
        .order_by(Cue.image_index, Cue.y_ratio, Cue.sort_order, Cue.id)
    ).all()
    cues = []
    for cue, object_key in rows:
        item = {
            "id": cue.id,
            "image_index": cue.image_index,
            "y_ratio": cue.y_ratio,
            "kind": cue.kind,
            "object_key": object_key,
            "duration_ms": cue.duration_ms,
        }
        if cue.kind == "sfx":
            item.update({"label": cue.label, "volume": cue.volume})
        else:
            item.update({"text": cue.text, "speaker": cue.speaker})
        cues.append(item)
    return {
        "language": series.language if series else "ko",
        "images": [
            {
                "index": img.position,
                "object_key": img.object_key,
                "width": img.width,
                "height": img.height,
                "alt": img.alt or "",
            }
            for img in images
        ],
        "cues": cues,
    }


def playback_view(data: dict, version: int) -> tuple[list[dict], dict]:
    """스냅샷 → (images, playback). 음원 URL은 audio TTL, 이미지 URL은 image TTL로 서명한다."""
    settings = get_settings()
    audio_ttl = settings.audio_url_ttl_seconds
    images = [
        {
            "index": img["index"],
            "url": signed_get(img["object_key"]),
            "width": img.get("width"),
            "height": img.get("height"),
            "alt": img.get("alt") or "",
        }
        for img in data.get("images", [])
    ]
    cues = []
    for cue in data.get("cues", []):
        out = {k: v for k, v in cue.items() if k != "object_key"}
        out["audio_url"] = signed_get(cue.get("object_key"), audio_ttl)
        cues.append(out)
    expires = (utcnow() + timedelta(seconds=audio_ttl)).replace(microsecond=0)
    return images, {"version": version, "urls_expire_at": expires, "cues": cues}


def transcript_items(data: dict) -> list[dict]:
    items = []
    for i, cue in enumerate(data.get("cues", []), start=1):
        if cue["kind"] == "sfx":
            items.append(
                {
                    "index": i,
                    "image_index": cue["image_index"],
                    "kind": "sfx",
                    "text": f"[{cue.get('label')}]",
                }
            )
        else:
            items.append(
                {
                    "index": i,
                    "image_index": cue["image_index"],
                    "kind": cue["kind"],
                    "speaker": cue.get("speaker") if cue["kind"] == "dialogue" else None,
                    "text": cue.get("text") or "",
                }
            )
    return items
