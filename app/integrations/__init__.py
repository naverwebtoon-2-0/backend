"""외부 연동 묶음. 설정(AI_BACKEND, STORAGE_BACKEND)에 따라 실제/가짜 구현을 고른다.

테스트는 `set_integrations()`로 가짜 구현을 직접 주입한다.
"""

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.integrations.dubbing import ClovaDubbingClient, DubbingClient, FakeDubbingClient
from app.integrations.ocr import ClovaOcrClient, FakeOcrClient, OcrClient
from app.integrations.storage import FakeStorage, S3StorageClient, StorageClient
from app.integrations.structure import ClovaStructureClient, FakeStructureClient, StructureClient


@dataclass
class Integrations:
    storage: StorageClient
    ocr: OcrClient
    structure: StructureClient
    dubbing: DubbingClient


_current: Integrations | None = None


def build_integrations(settings: Settings) -> Integrations:
    if settings.storage_backend == "s3":
        storage: StorageClient = S3StorageClient(settings)
    else:
        storage = FakeStorage(settings.fake_storage_dir, settings.public_base_url, settings.jwt_secret)
    if settings.ai_backend == "clova":
        dubbing: DubbingClient = ClovaDubbingClient(settings)
        tags = [p["tag"] for p in dubbing.list_sfx_presets()]
        return Integrations(storage, ClovaOcrClient(settings), ClovaStructureClient(settings, tags), dubbing)
    return Integrations(storage, FakeOcrClient(), FakeStructureClient(), FakeDubbingClient())


def get_integrations() -> Integrations:
    global _current
    if _current is None:
        _current = build_integrations(get_settings())
    return _current


def set_integrations(value: Integrations | None) -> None:
    global _current
    _current = value
