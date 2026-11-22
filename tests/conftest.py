"""테스트는 실제 PostgreSQL을 쓴다 (TEST_DATABASE_URL). 외부 서비스는 Fake 구현."""

import os
import struct
import tempfile
import zlib
from collections.abc import Iterator

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5433/nw_test")
_TMP = tempfile.mkdtemp(prefix="nw-test-")
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DB,
        "AUTHOR_INVITE_CODES": "invite-123,invite-456",
        "FAKE_STORAGE_DIR": os.path.join(_TMP, "storage"),
        "PUBLIC_BASE_URL": "http://testserver",
        "STORAGE_BACKEND": "fake",
        "AI_BACKEND": "fake",
        "JWT_SECRET": "test-secret-test-secret-test-secret-123",
        "LOG_LEVEL": "WARNING",
        "QUOTA_DUBBING_CHARS": "100000",
        "QUOTA_OCR_IMAGES": "500",
    }
)

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, get_sessionmaker  # noqa: E402
from app.integrations import Integrations, set_integrations  # noqa: E402
from app.integrations.dubbing import FakeDubbingClient  # noqa: E402
from app.integrations.ocr import FakeOcrClient  # noqa: E402
from app.integrations.storage import FakeStorage  # noqa: E402
from app.integrations.structure import FakeStructureClient  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    engine = create_engine(TEST_DB)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.attributes["database_url"] = TEST_DB
    cfg.attributes["skip_logging"] = True
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_db(_schema: None) -> Iterator[None]:
    from app.api.v1.auth import login_limiter

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_sessionmaker()() as s:
        s.execute(text(f"TRUNCATE {tables} CASCADE"))
        s.commit()
    login_limiter.clear()
    yield


@pytest.fixture
def integrations(tmp_path) -> Iterator[Integrations]:
    settings = get_settings()
    value = Integrations(
        storage=FakeStorage(tmp_path / "storage", settings.public_base_url, settings.jwt_secret),
        ocr=FakeOcrClient(),
        structure=FakeStructureClient(),
        dubbing=FakeDubbingClient(),
    )
    set_integrations(value)
    yield value
    set_integrations(None)


@pytest.fixture
def client(integrations: Integrations) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def db() -> Iterator[Session]:
    with get_sessionmaker()() as s:
        yield s


# ── 헬퍼 ───────────────────────────────────────────────


def make_png(width: int = 800, height: int = 1200) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def signup(client: TestClient, email: str, *, author: bool = True, password: str = "password123") -> dict:
    body = {"email": email, "password": password, "nickname": email.split("@")[0]}
    if author:
        body["invite_code"] = "invite-123"
    res = client.post("/v1/auth/signup", json=body)
    assert res.status_code == 201, res.text
    res = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def create_series(client: TestClient, headers: dict, **overrides) -> dict:
    body = {"title": "별빛 아래 우리", "description": "소개", "genre": "fantasy", **overrides}
    res = client.post("/v1/series", json=body, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def create_episode(client: TestClient, headers: dict, series_id: str, title: str = "1화") -> dict:
    res = client.post(f"/v1/series/{series_id}/episodes", json={"title": title}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def upload_images(client: TestClient, headers: dict, episode_id: str, count: int = 2) -> list[str]:
    files = [make_png(800, 1200 + i) for i in range(count)]
    res = client.post(
        f"/v1/episodes/{episode_id}/images/presign",
        json={
            "files": [
                {"filename": f"{i:03}.png", "content_type": "image/png", "size": len(f)}
                for i, f in enumerate(files)
            ]
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    uploads = res.json()["uploads"]
    for upload, data in zip(uploads, files, strict=True):
        put = client.put(upload["url"], content=data, headers=upload["headers"])
        assert put.status_code == 200, put.text
    ids = [u["image_id"] for u in uploads]
    res = client.post(f"/v1/episodes/{episode_id}/images/complete", json={"image_ids": ids}, headers=headers)
    assert res.status_code == 200, res.text
    return ids
