import hmac

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import ApiError, unauthorized, validation_error
from app.models import RefreshToken, User
from app.ratelimit import SlidingWindowLimiter
from app.schemas.auth import LoginRequest, RefreshRequest, SignupRequest, SignupResponse, TokenResponse
from app.schemas.common import ERROR_RESPONSES
from app.security import (
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)
from app.services.episodes import utcnow
from app.services.views import user_view

router = APIRouter(prefix="/v1/auth", tags=["auth"], responses=ERROR_RESPONSES)

_settings = get_settings()
login_limiter = SlidingWindowLimiter(_settings.login_max_attempts, _settings.login_window_seconds)


def _issue_tokens(db: Session, user: User) -> dict:
    token, token_hash, expires_at = new_refresh_token()
    db.add(RefreshToken(user_id=user.id, token_hash=token_hash, expires_at=expires_at))
    settings = get_settings()
    return {
        "access_token": create_access_token(user.id, user.role),
        "refresh_token": token,
        "expires_in": settings.access_token_ttl_seconds,
        "token_type": "Bearer",
        "_hash": token_hash,
    }


def _is_valid_invite(code: str) -> bool:
    return any(hmac.compare_digest(code, valid) for valid in get_settings().author_invite_codes)


@router.post("/signup", status_code=201, response_model=SignupResponse)
def signup(body: SignupRequest, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if len(body.password) < settings.password_min_length:
        raise validation_error(
            f"비밀번호는 {settings.password_min_length}자 이상이어야 합니다.", field="password"
        )
    role = "reader"
    if body.invite_code:
        if not _is_valid_invite(body.invite_code):
            raise validation_error("초대 코드가 올바르지 않습니다.", field="invite_code")
        role = "author"
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        nickname=body.nickname.strip(),
        role=role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "EMAIL_ALREADY_EXISTS", "이미 가입된 이메일입니다.") from exc
    return {"user": user_view(user)}


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    email = body.email.lower()
    client = request.client.host if request.client else "unknown"
    key = f"{client}|{email}"
    retry = login_limiter.retry_after(key)
    if retry is not None:
        raise ApiError(
            429,
            "TOO_MANY_ATTEMPTS",
            "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
            {"retry_after": retry},
            headers={"Retry-After": str(retry)},
        )
    user = db.scalar(select(User).where(User.email == email))
    if not verify_password(user.password_hash if user else None, body.password) or user is None:
        login_limiter.hit(key)
        raise unauthorized("이메일 또는 비밀번호가 올바르지 않습니다.")
    login_limiter.reset(key)
    tokens = _issue_tokens(db, user)
    db.commit()
    tokens.pop("_hash")
    return tokens


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> dict:
    row = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(body.refresh_token))
        .with_for_update()
    )
    now = utcnow()
    if row is None:
        raise unauthorized("refresh 토큰이 유효하지 않습니다.")
    if row.revoked_at is not None:
        # 이미 교체된 토큰이 다시 쓰였다 → 탈취로 보고 그 사용자의 모든 refresh 토큰을 폐기한다.
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == row.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        db.commit()
        raise unauthorized("refresh 토큰이 유효하지 않습니다.")
    if row.expires_at <= now:
        raise unauthorized("refresh 토큰이 만료되었습니다.")
    user = db.get(User, row.user_id)
    if user is None:
        raise unauthorized("refresh 토큰이 유효하지 않습니다.")
    tokens = _issue_tokens(db, user)
    db.flush()
    new_row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == tokens.pop("_hash")))
    row.revoked_at = now
    row.replaced_by_id = new_row.id if new_row else None
    db.commit()
    return tokens


@router.post("/logout", status_code=204)
def logout(body: RefreshRequest, db: Session = Depends(get_db)) -> Response:
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))
    if row is not None and row.revoked_at is None:
        row.revoked_at = utcnow()
        db.commit()
    return Response(status_code=204)
