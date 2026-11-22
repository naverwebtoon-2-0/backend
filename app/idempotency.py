"""Idempotency-Key 처리 (생성·재생성 계열 POST).

같은 사용자·같은 키로 같은 요청이 다시 오면 저장해 둔 응답을 그대로 돌려준다.
같은 키로 다른 요청이 오면 409 IDEMPOTENCY_KEY_REUSED.
"""

import hashlib
import json
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError, validation_error
from app.jsonutil import encode
from app.models import IdempotencyKey, User


class Idempotency:
    def __init__(self, db: Session, user: User, request: Request, body: Any = None) -> None:
        self.db = db
        self.user_id = user.id
        self.key = request.headers.get("idempotency-key")
        if self.key is not None and not (1 <= len(self.key) <= 255):
            raise validation_error("Idempotency-Key 길이가 올바르지 않습니다.", field="Idempotency-Key")
        self.method = request.method
        self.path = request.url.path
        payload = json.dumps(encode(body), sort_keys=True, ensure_ascii=False)
        self.request_hash = hashlib.sha256(f"{self.method} {self.path}\n{payload}".encode()).hexdigest()

    def _lookup(self) -> IdempotencyKey | None:
        return self.db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == self.user_id, IdempotencyKey.key == self.key
            )
        )

    def _response(self, row: IdempotencyKey) -> JSONResponse:
        if row.request_hash != self.request_hash:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "같은 Idempotency-Key로 다른 요청을 보냈습니다.")
        return JSONResponse(
            row.response, status_code=row.status_code, headers={"Idempotent-Replayed": "true"}
        )

    def replay(self) -> JSONResponse | None:
        if self.key is None:
            return None
        row = self._lookup()
        return self._response(row) if row else None

    def commit(self, status_code: int, content: Any) -> JSONResponse:
        """결과를 키와 함께 저장하고 커밋한다. 동시에 같은 키가 처리되었으면 그 응답을 돌려준다."""
        encoded = encode(content)
        if self.key is not None:
            self.db.add(
                IdempotencyKey(
                    user_id=self.user_id,
                    key=self.key,
                    method=self.method,
                    path=self.path,
                    request_hash=self.request_hash,
                    status_code=status_code,
                    response=encoded,
                )
            )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            row = self._lookup() if self.key is not None else None
            if row is None:
                raise
            return self._response(row)
        return JSONResponse(encoded, status_code=status_code)
