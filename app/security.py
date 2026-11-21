import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

_hasher = PasswordHasher()
# 존재하지 않는 계정으로 로그인해도 같은 시간이 걸리도록 비교용 해시를 둔다.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: str, role: str) -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "role": role,
        "typ": "access",
        "iat": now,
        "exp": now + settings.access_token_ttl_seconds,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(
            token, get_settings().jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "access":
        return None
    return payload


def new_refresh_token() -> tuple[str, str, datetime]:
    """(평문 토큰, DB 저장용 해시, 만료 시각)"""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=get_settings().refresh_token_ttl_days)
    return token, hash_token(token), expires_at


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
