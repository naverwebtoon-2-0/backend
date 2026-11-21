"""개발용 가짜 Object Storage 엔드포인트 (STORAGE_BACKEND=fake, 운영 비활성).

presigned URL 형식: /_fake-storage/{key}?exp=...&sig=...
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from app.errors import ApiError, not_found
from app.integrations import get_integrations
from app.integrations.storage import FakeStorage

router = APIRouter(prefix="/_fake-storage", include_in_schema=False)


def _storage() -> FakeStorage:
    storage = get_integrations().storage
    if not isinstance(storage, FakeStorage):
        raise not_found()
    return storage


def _check(storage: FakeStorage, method: str, key: str, request: Request) -> None:
    try:
        exp = int(request.query_params.get("exp", "0"))
    except ValueError:
        exp = 0
    if not storage.verify(method, key, exp, request.query_params.get("sig", "")):
        raise ApiError(403, "FORBIDDEN", "서명이 올바르지 않거나 만료되었습니다.")


@router.put("/{key:path}")
async def put_object(key: str, request: Request) -> Response:
    storage = _storage()
    _check(storage, "PUT", key, request)
    data = await request.body()
    storage.put(key, data, request.headers.get("content-type", "application/octet-stream"))
    return Response(status_code=200)


@router.get("/{key:path}")
def get_object(key: str, request: Request) -> Response:
    storage = _storage()
    _check(storage, "GET", key, request)
    info = storage.head(key)
    if info is None:
        raise not_found()
    return FileResponse(storage._path(key), media_type=info.content_type or "application/octet-stream")
