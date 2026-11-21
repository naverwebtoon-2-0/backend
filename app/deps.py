from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import forbidden, unauthorized
from app.models import User
from app.security import decode_access_token


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = _bearer(request)
    if token is None:
        return None
    payload = decode_access_token(token)
    if payload is None:
        raise unauthorized("토큰이 없거나 만료되었습니다.")
    user = db.get(User, payload["sub"])
    if user is None:
        raise unauthorized("토큰이 없거나 만료되었습니다.")
    return user


def get_current_user(user: User | None = Depends(get_optional_user)) -> User:
    if user is None:
        raise unauthorized()
    return user


def require_author(user: User = Depends(get_current_user)) -> User:
    if user.role != "author":
        raise forbidden("작가 계정만 사용할 수 있습니다.")
    return user
