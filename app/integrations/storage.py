"""Object Storage 추상화.

- S3StorageClient: Naver Object Storage (S3 호환, boto3, presigned PUT/GET)
- FakeStorage: 로컬 디렉터리. 개발용 `/_fake-storage/*` 경로가 presigned URL 역할을 한다.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from app.config import Settings
from app.integrations.http import UpstreamError, diagnostic_id


@contextmanager
def _storage_error(operation: str) -> Iterator[None]:
    try:
        yield
    except ClientError as exc:
        error = exc.response.get("Error", {})
        meta = exc.response.get("ResponseMetadata", {})
        status = meta.get("HTTPStatusCode")
        raise UpstreamError(
            "object_storage",
            error.get("Message") or "Object Storage 요청이 거부됐습니다.",
            status == 429 or (isinstance(status, int) and status >= 500),
            status=status,
            code=diagnostic_id(error.get("Code")),
            request_id=diagnostic_id(meta.get("RequestId")),
            operation=operation,
        ) from exc
    except BotoCoreError as exc:
        code = type(exc).__name__
        raise UpstreamError(
            "object_storage", code, not isinstance(exc, ParamValidationError), code=code, operation=operation
        ) from exc


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


class StorageClient(Protocol):
    def presign_put(self, key: str, content_type: str, expires_in: int) -> str: ...
    def presign_get(self, key: str, expires_in: int) -> str: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def get_range(self, key: str, length: int) -> bytes: ...
    def get(self, key: str) -> bytes: ...
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def delete(self, key: str) -> None: ...


class S3StorageClient:
    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.storage_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint,
            region_name=settings.storage_region,
            aws_access_key_id=settings.storage_access_key,
            aws_secret_access_key=settings.storage_secret_key,
            config=Config(
                signature_version="s3v4",
                # 네이버 Object Storage는 SDK의 기본 체크섬 형식을 지원하지 않는다.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def presign_put(self, key: str, content_type: str, expires_in: int) -> str:
        with _storage_error("presign_put"):
            return self.client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )

    def presign_get(self, key: str, expires_in: int) -> str:
        with _storage_error("presign_get"):
            return self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
            )

    def head(self, key: str) -> ObjectInfo | None:
        with _storage_error("head_object"):
            try:
                res = self.client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise
        return ObjectInfo(size=int(res["ContentLength"]), content_type=res.get("ContentType"))

    def get_range(self, key: str, length: int) -> bytes:
        with _storage_error("get_object_range"):
            res = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}")
            with closing(res["Body"]) as body:
                return body.read()

    def get(self, key: str) -> bytes:
        with (
            _storage_error("get_object"),
            closing(self.client.get_object(Bucket=self.bucket, Key=key)["Body"]) as body,
        ):
            return body.read()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        with _storage_error("put_object"):
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def delete(self, key: str) -> None:
        with _storage_error("delete_object"):
            self.client.delete_object(Bucket=self.bucket, Key=key)


class FakeStorage:
    """파일 시스템 기반 가짜 저장소 (api·worker 프로세스가 같은 디렉터리를 공유)."""

    def __init__(self, root: str | Path, base_url: str, secret: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self._secret = secret.encode()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("invalid key")
        return path

    def sign(self, method: str, key: str, exp: int) -> str:
        msg = f"{method}\n{key}\n{exp}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify(self, method: str, key: str, exp: int, sig: str) -> bool:
        return exp >= int(time.time()) and hmac.compare_digest(self.sign(method, key, exp), sig)

    def _url(self, method: str, key: str, expires_in: int) -> str:
        exp = int(time.time()) + expires_in
        query = urlencode({"exp": exp, "sig": self.sign(method, key, exp)})
        return f"{self.base_url}/_fake-storage/{quote(key)}?{query}"

    def presign_put(self, key: str, content_type: str, expires_in: int) -> str:
        return self._url("PUT", key, expires_in)

    def presign_get(self, key: str, expires_in: int) -> str:
        return self._url("GET", key, expires_in)

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        meta_path = path.with_name(path.name + ".meta.json")
        content_type = json.loads(meta_path.read_text())["content_type"] if meta_path.exists() else None
        return ObjectInfo(size=path.stat().st_size, content_type=content_type)

    def get_range(self, key: str, length: int) -> bytes:
        with self._path(key).open("rb") as fp:
            return fp.read(length)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.with_name(path.name + ".meta.json").write_text(json.dumps({"content_type": content_type}))

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".meta.json").unlink(missing_ok=True)
