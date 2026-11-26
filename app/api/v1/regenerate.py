"""재생성 (§7): 수정한 대사만 다시 만든다. 동일 입력은 음원 캐시를 재사용한다."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_author
from app.errors import ApiError, job_in_progress, validation_error
from app.idempotency import Idempotency
from app.models import Character, DialogueLine, Episode, Series, User
from app.models.studio import VOICE_KINDS
from app.schemas.common import ERROR_RESPONSES
from app.schemas.studio import LineAudioResponse, LineRegenerateRequest, RegenerateRequest, RegenerateResponse
from app.services import quota
from app.services.access import episode_for_author, line_for_author
from app.services.audio import find_asset, voice_input
from app.services.episodes import (
    ensure_editable,
    ensure_no_active_job,
    mark_changed,
    narration_character,
    recompute_stats,
)
from app.services.jobs import create_job
from app.services.views import load_line_views

router = APIRouter(tags=["regenerate"], responses={**ERROR_RESPONSES, 429: {"description": "QUOTA_EXCEEDED"}})


def _uncached_chars(db: Session, episode: Episode, lines: list[DialogueLine], force: bool) -> int:
    characters = {
        c.id: c for c in db.scalars(select(Character).where(Character.series_id == episode.series_id))
    }
    narrator = narration_character(db, episode.series_id)
    total = 0
    for line in lines:
        vin = voice_input(line, characters, narrator)
        if force or find_asset(db, vin.hash) is None:
            total += len(vin.text)
    return total


def _start_job(db: Session, episode: Episode, user: User, lines: list[DialogueLine], force: bool) -> dict:
    series = db.get(Series, episode.series_id)
    quota.ensure_available(
        db, series.author_id, quota.DUBBING_CHARS, _uncached_chars(db, episode, lines, force)
    )
    job = create_job(
        db,
        episode,
        kind="regenerate",
        step="dubbing",
        payload={"line_ids": [line.id for line in lines], "force": force},
        user=user,
    )
    for line in lines:
        line.audio_status = "generating"
    recompute_stats(db, episode)
    return {"job": {"id": job.id, "kind": job.kind, "status": job.status, "step": job.step}}


@router.post("/v1/episodes/{episode_id}/regenerate", status_code=202, response_model=RegenerateResponse)
def regenerate_episode(
    episode_id: str,
    body: RegenerateRequest,
    request: Request,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    episode, _ = episode_for_author(db, episode_id, user, lock=True)
    ensure_editable(episode)
    ensure_no_active_job(db, episode.id)
    stmt = select(DialogueLine).where(
        DialogueLine.episode_id == episode.id, DialogueLine.kind.in_(VOICE_KINDS)
    )
    if body.scope == "stale":
        lines = list(db.scalars(stmt.where(DialogueLine.audio_status.in_(("stale", "failed")))))
    elif body.scope == "lines":
        if not body.line_ids:
            raise validation_error("scope=lines에는 line_ids가 필요합니다.", field="line_ids")
        ids = list(dict.fromkeys(body.line_ids))
        found = {line.id: line for line in db.scalars(stmt.where(DialogueLine.id.in_(ids)))}
        missing = [i for i in ids if i not in found]
        if missing:
            raise validation_error("재생성할 수 없는 대사가 있습니다.", line_ids=missing)
        lines = [found[i] for i in ids]
    else:
        lines = list(db.scalars(stmt))
    if body.scope != "lines" and body.line_ids:
        raise validation_error("line_ids는 scope=lines에서만 쓸 수 있습니다.", field="line_ids")
    if not lines:
        raise validation_error("재생성할 대사가 없습니다.", scope=body.scope)
    content = _start_job(db, episode, user, lines, force=False)
    return idem.commit(202, content)


@router.post(
    "/v1/lines/{line_id}/regenerate",
    status_code=202,
    response_model=RegenerateResponse,
    responses={200: {"model": LineAudioResponse, "description": "캐시된 음원 반환"}},
)
def regenerate_line(
    line_id: str,
    request: Request,
    body: LineRegenerateRequest | None = None,
    user: User = Depends(require_author),
    db: Session = Depends(get_db),
):
    body = body or LineRegenerateRequest()
    idem = Idempotency(db, user, request, body)
    if (replayed := idem.replay()) is not None:
        return replayed
    line, episode = line_for_author(db, line_id, user)
    ensure_editable(episode)
    if line.kind not in VOICE_KINDS:
        raise ApiError(400, "VALIDATION_ERROR", "음성 생성 대상이 아닌 대사입니다.", {"kind": line.kind})
    if line.audio_status == "generating":
        raise job_in_progress()
    if not body.force:
        characters = {
            c.id: c for c in db.scalars(select(Character).where(Character.series_id == episode.series_id))
        }
        vin = voice_input(line, characters, narration_character(db, episode.series_id))
        cached = find_asset(db, vin.hash)
        if cached is not None:
            # 캐시 적중: 비용 없이 즉시 연결 → 200
            changed = line.audio_asset_id != cached.id or line.audio_status != "ready"
            line.audio_asset_id = cached.id
            line.audio_input_hash = vin.hash
            line.audio_status = "ready"
            if changed:
                mark_changed(episode)
            recompute_stats(db, episode)
            db.flush()
            return idem.commit(200, {"audio": load_line_views(db, [line])[0]["audio"]})
    ensure_no_active_job(db, episode.id)
    content = _start_job(db, episode, user, [line], force=body.force)
    return idem.commit(202, content)
