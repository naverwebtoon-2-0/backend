from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import forbidden, unauthorized
from app.models import User
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", bearerFormat="JWT")


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    token = credentials.credentials.strip() if credentials else ""
    if not token:
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
