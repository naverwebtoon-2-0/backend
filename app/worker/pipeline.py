"""파이프라인 실행: ocr → structure → dubbing → build_cues (process), dubbing → build_cues (regenerate).

- 단계·항목마다 커밋해 진행률과 결과를 남긴다. 실패 후 재시도하면 마지막 성공 단계 다음부터 이어 간다.
- 외부 호출(OCR·Dubbing)은 트랜잭션 밖에서 하고, 결과를 반영할 때 행을 다시 읽어 잠근다.
- 취소 요청(cancel_requested)은 항목 사이마다 확인한다.
"""

import logging
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.integrations import Integrations
from app.integrations.dubbing import VOICES
from app.integrations.http import UpstreamError
from app.integrations.structure import PageInput, structure_from_ocr
from app.models import Character, Cue, DialogueLine, Episode, EpisodeImage, Job, Series, SfxCue
from app.models.episode import PIPELINE_STEPS
from app.models.studio import EMOTIONS, LINE_KINDS, VOICE_KINDS
from app.services import quota
from app.services.audio import find_asset, sfx_asset, synthesize_voice, voice_input
from app.services.episodes import mark_changed, narration_character, recompute_stats, utcnow
from app.services.jobs import finish_canceled, release_generating
from app.services.playback import rebuild_cues

logger = logging.getLogger(__name__)


class Canceled(Exception):
    pass


class StepFailed(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class JobRunner:
    def __init__(self, session_factory: sessionmaker[Session], integrations: Integrations) -> None:
        self.session_factory = session_factory
        self.integrations = integrations

    # ── 공통 ───────────────────────────────────────────

    def _touch(self, s: Session, job_id: str, **values: object) -> None:
        """진행률·heartbeat 갱신 후 취소 요청 확인."""
        values["heartbeat_at"] = utcnow()
        s.execute(update(Job).where(Job.id == job_id).values(**values))
        s.commit()
        if s.scalar(select(Job.cancel_requested).where(Job.id == job_id)):
            raise Canceled

    def _begin_step(self, job_id: str, step: str, total: int, episode_state: str | None = None) -> None:
        with self.session_factory() as s:
            if episode_state:
                job = s.get(Job, job_id)
                episode = s.get(Episode, job.episode_id, with_for_update=True)
                episode.state = episode_state
                episode.updated_at = utcnow()
            self._touch(s, job_id, step=step, progress_done=0, progress_total=total, step_started_at=utcnow())

    def _end_step(self, job_id: str, step: str, *, pipeline: bool) -> None:
        with self.session_factory() as s:
            job = s.get(Job, job_id, with_for_update=True)
            if job.step_started_at and job.progress_total:
                elapsed = (utcnow() - job.step_started_at).total_seconds()
                job.step_rates = {**job.step_rates, step: round(elapsed / job.progress_total, 3)}
            job.progress_done = job.progress_total
            if pipeline:
                episode = s.get(Episode, job.episode_id, with_for_update=True)
                episode.pipeline_last_step = step
            s.commit()

    def run(self, job_id: str) -> None:
        with self.session_factory() as s:
            job = s.get(Job, job_id)
            kind, start = job.kind, job.step
        try:
            if kind == "process":
                self._run_process(job_id, start)
            else:
                self._run_regenerate(job_id)
        except Canceled:
            with self.session_factory() as s:
                job = s.get(Job, job_id, with_for_update=True)
                episode = s.get(Episode, job.episode_id, with_for_update=True)
                finish_canceled(s, job, episode)
                recompute_stats(s, episode)
                s.commit()
            logger.info("job canceled", extra={"job_id": job_id})
        except StepFailed as exc:
            self._fail(job_id, exc.code, exc.message, exc.details)
        except UpstreamError as exc:
            self._fail(job_id, exc.app_code, exc.message, exc.details())
        except Exception as exc:
            logger.exception("job crashed", extra={"job_id": job_id})
            self._fail(job_id, "INTERNAL_ERROR", "작업 중 오류가 발생했습니다.", {"type": type(exc).__name__})

    def _fail(self, job_id: str, code: str, message: str, details: dict | None) -> None:
        with self.session_factory() as s:
            job = s.get(Job, job_id, with_for_update=True)
            episode = s.get(Episode, job.episode_id, with_for_update=True)
            job.status = "failed"
            job.finished_at = utcnow()
            job.error = {
                "code": code,
                "message": message,
                "details": {"reason": message, **(details or {}), "step": job.step},
            }
            release_generating(s, job)
            if job.kind == "process":
                episode.state = "failed"
            episode.updated_at = utcnow()
            recompute_stats(s, episode)
            s.commit()
        logger.warning("job failed", extra={"job_id": job_id, "code": code, "details": details})

    def _record_warning(self, s: Session, job_id: str, warning: dict) -> None:
        job = s.get(Job, job_id, with_for_update=True)
        job.warnings = [*(job.warnings or []), warning]
        logger.warning("job item failed", extra={"job_id": job_id, "warning": warning})

    # ── process ────────────────────────────────────────

    def _run_process(self, job_id: str, start: str) -> None:
        steps: dict[str, Callable[[str], None]] = {
            "ocr": self._step_ocr,
            "structure": self._step_structure,
            "dubbing": self._step_dubbing,
            "build_cues": self._step_build_cues,
        }
        for step in PIPELINE_STEPS[PIPELINE_STEPS.index(start) :]:
            steps[step](job_id)
            self._end_step(job_id, step, pipeline=True)
        with self.session_factory() as s:
            job = s.get(Job, job_id, with_for_update=True)
            episode = s.get(Episode, job.episode_id, with_for_update=True)
            job.status = "succeeded"
            job.step = "done"
            job.finished_at = utcnow()
            episode.state = "needs_review"
            episode.updated_at = utcnow()
            recompute_stats(s, episode)
            s.commit()

    def _episode_ctx(self, s: Session, job_id: str) -> tuple[Job, Episode, Series]:
        job = s.get(Job, job_id)
        episode = s.get(Episode, job.episode_id)
        return job, episode, s.get(Series, episode.series_id)

    def _images(self, s: Session, episode_id: str) -> list[EpisodeImage]:
        return list(
            s.scalars(
                select(EpisodeImage)
                .where(EpisodeImage.episode_id == episode_id, EpisodeImage.upload_status == "uploaded")
                .order_by(EpisodeImage.position)
            )
        )

    def _step_ocr(self, job_id: str) -> None:
        with self.session_factory() as s:
            _, episode, series = self._episode_ctx(s, job_id)
            images = [
                (img.id, img.object_key, img.content_type, img.width, img.height, img.ocr_result)
                for img in self._images(s, episode.id)
            ]
            language = episode.process_options.get("ocr_language", "ko")
            author_id = series.author_id
        self._begin_step(job_id, "ocr", len(images), "analyzing")
        for done, (image_id, key, content_type, width, height, existing) in enumerate(images, start=1):
            if existing is None:
                with self.session_factory() as s:
                    if quota.remaining(s, author_id, quota.OCR_IMAGES) < 1:
                        raise StepFailed(
                            "QUOTA_EXCEEDED", "OCR 쿼터를 초과했습니다.", {"metric": quota.OCR_IMAGES}
                        )
                try:
                    data = self.integrations.storage.get(key)
                    fields = self.integrations.ocr.recognize(
                        data, content_type, width or 0, height or 0, language
                    )
                except UpstreamError as exc:
                    raise StepFailed(
                        exc.app_code,
                        "이미지 OCR 처리에 실패했습니다.",
                        {**exc.details(), "image_id": image_id, "width": width, "height": height},
                    ) from exc
                with self.session_factory() as s:
                    s.execute(
                        update(EpisodeImage)
                        .where(EpisodeImage.id == image_id)
                        .values(
                            ocr_result=[
                                {"text": f.text, "bbox": f.bbox, "confidence": f.confidence} for f in fields
                            ]
                        )
                    )
                    quota.consume(s, author_id, quota.OCR_IMAGES, 1)
                    self._touch(s, job_id, progress_done=done)
            else:
                with self.session_factory() as s:
                    self._touch(s, job_id, progress_done=done)

    def _step_structure(self, job_id: str) -> None:
        with self.session_factory() as s:
            _, episode, series = self._episode_ctx(s, job_id)
            images = self._images(s, episode.id)
            pages = [PageInput(img.id, img.ocr_result or []) for img in images]
            characters = list(s.scalars(select(Character).where(Character.series_id == series.id)))
            options = dict(episode.process_options)
        self._begin_step(job_id, "structure", len(pages), "analyzing")
        fallback_warning = None
        try:
            result = self.integrations.structure.structure(
                pages, [c.name for c in characters if c.kind == "character"], options
            )
        except UpstreamError as exc:
            if exc.code != "INVALID_RESPONSE" or exc.status != 200:
                raise StepFailed(exc.app_code, "CLOVA Studio 호출에 실패했습니다.", exc.details()) from exc
            result = structure_from_ocr(pages)
            fallback_warning = {
                "code": exc.app_code,
                "message": "CLOVA Studio 응답 형식이 올바르지 않아 OCR 원문으로 대사를 만들었습니다.",
                "details": {**exc.details(), "step": "structure"},
            }

        with self.session_factory() as s:
            _, episode, series = self._episode_ctx(s, job_id)
            episode = s.get(Episode, episode.id, with_for_update=True)
            if fallback_warning:
                self._record_warning(s, job_id, fallback_warning)
            # 다시 정리하면 이전 결과를 대체한다.
            s.execute(delete(Cue).where(Cue.episode_id == episode.id))
            s.execute(delete(SfxCue).where(SfxCue.episode_id == episode.id))
            s.execute(delete(DialogueLine).where(DialogueLine.episode_id == episode.id))
            by_name = {
                c.name: c for c in s.scalars(select(Character).where(Character.series_id == series.id))
            }
            voice_pool = [v["id"] for v in VOICES if v["id"] != "nara"]
            orders: dict[tuple[str, float], int] = {}
            valid_images = {p.image_id for p in pages}
            for item in result.lines:
                if item.image_id not in valid_images:
                    continue
                kind = item.kind if item.kind in LINE_KINDS else "dialogue"
                if kind == "description" and not options.get("description", True):
                    kind = "ignore"
                character_id = None
                if item.speaker and kind in VOICE_KINDS:
                    character = by_name.get(item.speaker)
                    if character is None:
                        character = Character(
                            series_id=series.id,
                            name=item.speaker[:100],
                            kind="character",
                            voice_id=voice_pool[len(by_name) % len(voice_pool)],
                        )
                        s.add(character)
                        s.flush()
                        by_name[character.name] = character
                    character_id = character.id
                y = float(item.bbox["y"]) if item.bbox and "y" in item.bbox else 0.0
                y = min(1.0, max(0.0, y))
                key = (item.image_id, y)
                orders[key] = orders.get(key, 0) + 1
                s.add(
                    DialogueLine(
                        episode_id=episode.id,
                        image_id=item.image_id,
                        sort_order=orders[key],
                        text=item.text,
                        original_text=item.text,
                        bbox=item.bbox,
                        y_ratio=y,
                        kind=kind,
                        character_id=character_id,
                        emotion=item.emotion if item.emotion in EMOTIONS else "neutral",
                        ocr_confidence=item.ocr_confidence,
                        audio_status="generating" if kind in VOICE_KINDS else None,
                    )
                )
            for sfx in result.sfx:
                if sfx.image_id in valid_images:
                    s.add(
                        SfxCue(
                            episode_id=episode.id,
                            image_id=sfx.image_id,
                            y_ratio=min(1.0, max(0.0, sfx.y_ratio)),
                            sfx_tag=sfx.sfx_tag,
                            volume=1.0,
                        )
                    )
            recompute_stats(s, episode)
            self._touch(s, job_id, progress_done=len(pages))

    def _step_dubbing(self, job_id: str) -> None:
        with self.session_factory() as s:
            _, episode, _ = self._episode_ctx(s, job_id)
            line_ids = list(
                s.scalars(
                    select(DialogueLine.id).where(
                        DialogueLine.episode_id == episode.id,
                        DialogueLine.kind.in_(VOICE_KINDS),
                        DialogueLine.audio_status != "ready",
                    )
                )
            )
            s.execute(
                update(DialogueLine).where(DialogueLine.id.in_(line_ids)).values(audio_status="generating")
            )
            s.commit()
            sfx_ids = list(
                s.scalars(select(SfxCue.id).where(SfxCue.episode_id == episode.id, SfxCue.asset_id.is_(None)))
            )
        self._begin_step(job_id, "dubbing", len(line_ids) + len(sfx_ids), "synthesizing")
        done = self._synthesize_lines(job_id, line_ids, force=False)
        for cue_id in sfx_ids:
            with self.session_factory() as s:
                cue = s.get(SfxCue, cue_id, with_for_update=True)
                if cue is not None:
                    try:
                        asset = sfx_asset(s, self.integrations, cue.sfx_tag)
                        cue.asset_id, cue.duration_ms = asset.id, asset.duration_ms
                    except UpstreamError as exc:
                        s.rollback()
                        self._record_warning(
                            s,
                            job_id,
                            {
                                "code": exc.app_code,
                                "message": exc.message,
                                "details": {**exc.details(), "step": "dubbing", "cue_id": cue_id},
                            },
                        )
                done += 1
                self._touch(s, job_id, progress_done=done)

    def _synthesize_lines(self, job_id: str, line_ids: list[str], *, force: bool) -> int:
        """대사별 음성 생성. 일부 실패는 audio_status=failed로 남기고 계속한다. 처리한 개수를 돌려준다."""
        with self.session_factory() as s:
            _, episode, series = self._episode_ctx(s, job_id)
            author_id, series_id, episode_id = series.author_id, series.id, episode.id
        done = 0
        for line_id in line_ids:
            with self.session_factory() as s:
                line = s.get(DialogueLine, line_id)
                if line is None or line.kind not in VOICE_KINDS or line.audio_status != "generating":
                    done += 1
                    self._touch(s, job_id, progress_done=done)
                    continue
                characters = {
                    c.id: c for c in s.scalars(select(Character).where(Character.series_id == series_id))
                }
                vin = voice_input(line, characters, narration_character(s, series_id))
            asset_id, error = None, None
            with self.session_factory() as s:
                try:
                    cached = None if force else find_asset(s, vin.hash)
                    if cached is None:
                        if quota.remaining(s, author_id, quota.DUBBING_CHARS) < len(vin.text):
                            raise StepFailed("QUOTA_EXCEEDED", "음성 생성 쿼터를 초과했습니다.")
                        asset, _ = synthesize_voice(s, self.integrations, vin, force=force)
                        quota.consume(s, author_id, quota.DUBBING_CHARS, len(vin.text))
                    else:
                        asset = cached
                    asset_id = asset.id
                    s.commit()
                except (UpstreamError, StepFailed) as exc:
                    s.rollback()
                    code, details = (
                        (exc.app_code, exc.details())
                        if isinstance(exc, UpstreamError)
                        else (exc.code, exc.details or {})
                    )
                    error = {
                        "code": code,
                        "message": exc.message,
                        "details": {**details, "step": "dubbing", "line_id": line_id},
                    }
            with self.session_factory() as s:
                line = s.get(DialogueLine, line_id, with_for_update=True)
                if line is not None and line.audio_status == "generating":
                    characters = {
                        c.id: c for c in s.scalars(select(Character).where(Character.series_id == series_id))
                    }
                    current = voice_input(line, characters, narration_character(s, series_id))
                    if current.hash != vin.hash:
                        # 작업 중 수정됨 → 결과를 버리고 재생성 필요로 남긴다 (§7)
                        line.audio_status = "stale"
                        line.audio_error = None
                    elif error is not None:
                        line.audio_status = "failed"
                        line.audio_error = error
                        self._record_warning(s, job_id, error)
                    else:
                        line.audio_status = "ready"
                        line.audio_error = None
                        line.audio_asset_id = asset_id
                        line.audio_input_hash = vin.hash
                episode = s.get(Episode, episode_id)
                recompute_stats(s, episode)
                done += 1
                self._touch(s, job_id, progress_done=done)
        return done

    def _step_build_cues(self, job_id: str) -> None:
        self._begin_step(job_id, "build_cues", 1)
        with self.session_factory() as s:
            job = s.get(Job, job_id)
            episode = s.get(Episode, job.episode_id, with_for_update=True)
            rebuild_cues(s, episode)
            self._touch(s, job_id, progress_done=1)

    # ── regenerate ─────────────────────────────────────

    def _run_regenerate(self, job_id: str) -> None:
        with self.session_factory() as s:
            job = s.get(Job, job_id)
            line_ids = list(job.payload.get("line_ids", []))
            force = bool(job.payload.get("force", False))
        self._begin_step(job_id, "dubbing", len(line_ids))
        self._synthesize_lines(job_id, line_ids, force=force)
        self._end_step(job_id, "dubbing", pipeline=False)
        self._begin_step(job_id, "build_cues", 1)
        with self.session_factory() as s:
            job = s.get(Job, job_id, with_for_update=True)
            episode = s.get(Episode, job.episode_id, with_for_update=True)
            rebuild_cues(s, episode)
            recompute_stats(s, episode)
            mark_changed(episode)
            job.status = "succeeded"
            job.step = "done"
            job.progress_done = job.progress_total
            job.finished_at = utcnow()
            s.commit()


# ── 큐 조작 ──────────────────────────────────────────────


def claim_job(s: Session) -> str | None:
    """대기 중인 작업 하나를 SKIP LOCKED로 가져와 running으로 바꾼다."""
    job = s.scalar(
        select(Job)
        .where(Job.status == "queued")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        s.rollback()
        return None
    now = utcnow()
    job.status = "running"
    job.attempts += 1
    job.started_at = job.started_at or now
    job.heartbeat_at = now
    s.commit()
    return job.id


def reap_stale_jobs(s: Session, timeout_seconds: int, max_attempts: int) -> list[str]:
    """heartbeat가 끊긴(워커 비정상 종료) 작업을 다시 대기열로 돌리거나, 시도 횟수를 넘으면 실패 처리한다."""
    cutoff = utcnow() - timedelta(seconds=timeout_seconds)
    jobs = list(
        s.scalars(
            select(Job)
            .where(Job.status == "running", Job.heartbeat_at < cutoff)
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        episode = s.get(Episode, job.episode_id, with_for_update=True)
        if job.cancel_requested:
            finish_canceled(s, job, episode)
        elif job.attempts >= max_attempts:
            job.status = "failed"
            job.finished_at = utcnow()
            job.error = {
                "code": "WORKER_LOST",
                "message": "작업자가 응답하지 않아 작업을 중단했습니다.",
                "details": {"step": job.step, "attempts": job.attempts},
            }
            release_generating(s, job)
            if job.kind == "process":
                episode.state = "failed"
        else:
            # 같은 단계부터 다시 실행한다. process는 단계별 결과가 저장되어 있어 이어서 진행된다.
            job.status = "queued"
            if job.kind == "process" and episode.pipeline_last_step:
                i = PIPELINE_STEPS.index(episode.pipeline_last_step)
                job.step = PIPELINE_STEPS[min(i + 1, len(PIPELINE_STEPS) - 1)]
        logger.warning("reaped stale job", extra={"job_id": job.id, "status": job.status})
    s.commit()
    return [j.id for j in jobs]
