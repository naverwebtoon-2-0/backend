"""음성 입력 해석과 캐시 키. 캐시 키 = sha256(text + voice_id + voice_params + emotion) (§7)."""

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ids import new_id
from app.integrations import Integrations
from app.integrations.dubbing import DEFAULT_CHARACTER_VOICE, DEFAULT_NARRATION_VOICE
from app.models import AudioAsset, Character, DialogueLine


@dataclass(frozen=True)
class VoiceInput:
    text: str
    voice_id: str
    params: dict
    emotion: str

    @property
    def hash(self) -> str:
        return audio_hash(self.text, self.voice_id, self.params, self.emotion)


def audio_hash(text: str, voice_id: str, params: dict, emotion: str) -> str:
    raw = json.dumps([text, voice_id, params, emotion], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def voice_input(
    line: DialogueLine, characters: dict[str, Character], narrator: Character | None
) -> VoiceInput:
    """대사의 화자 목소리를 정한다. narration·description은 화자가 없으면 나레이션 인물을 쓴다."""
    character = characters.get(line.character_id) if line.character_id else None
    if character is None and line.kind in ("narration", "description"):
        character = narrator
    default = DEFAULT_CHARACTER_VOICE if line.kind == "dialogue" else DEFAULT_NARRATION_VOICE
    voice_id = (character.voice_id if character and character.voice_id else None) or default
    params = dict(character.voice_params) if character else {}
    params.pop("emotion_default", None)
    return VoiceInput(line.text, voice_id, params, line.emotion)


def find_asset(db: Session, hash_: str) -> AudioAsset | None:
    return db.scalar(select(AudioAsset).where(AudioAsset.hash == hash_))


def synthesize_voice(
    db: Session, integrations: Integrations, vin: VoiceInput, *, force: bool = False
) -> tuple[AudioAsset, bool]:
    """(asset, 새로 생성했는지).

    force면 캐시를 쓰지 않고 새 음원을 만들어, 같은 해시의 캐시 항목을 새 음원으로 교체한다.
    (이미 게시된 스냅샷은 object_key를 따로 저장하므로 영향을 받지 않는다.)
    """
    existing = find_asset(db, vin.hash)
    if existing is not None and not force:
        return existing, False
    result = integrations.dubbing.synthesize(vin.text, vin.voice_id, vin.params, vin.emotion)
    key = f"audio/voice/{vin.hash}-{new_id()[-12:]}.wav"
    integrations.storage.put(key, result.data, result.content_type)
    if existing is not None:
        existing.object_key = key
        existing.duration_ms = result.duration_ms
        existing.size = len(result.data)
        existing.content_type = result.content_type
        db.flush()
        return existing, True
    asset = AudioAsset(
        hash=vin.hash,
        kind="voice",
        object_key=key,
        content_type=result.content_type,
        size=len(result.data),
        duration_ms=result.duration_ms,
    )
    db.add(asset)
    db.flush()
    return asset, True


def sfx_asset(db: Session, integrations: Integrations, tag: str) -> AudioAsset:
    hash_ = hashlib.sha256(f"sfx:{tag}".encode()).hexdigest()
    existing = find_asset(db, hash_)
    if existing is not None:
        return existing
    result = integrations.dubbing.synthesize_sfx(tag)
    key = f"audio/sfx/{tag}-{hash_[:12]}.wav"
    integrations.storage.put(key, result.data, result.content_type)
    asset = AudioAsset(
        hash=hash_,
        kind="sfx",
        object_key=key,
        content_type=result.content_type,
        size=len(result.data),
        duration_ms=result.duration_ms,
    )
    db.add(asset)
    db.flush()
    return asset
