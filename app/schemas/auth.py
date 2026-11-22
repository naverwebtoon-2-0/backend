from typing import Any

from pydantic import EmailStr, Field

from app.schemas.common import StrictModel


class SignupRequest(StrictModel):
    email: EmailStr
    password: str = Field(max_length=128)
    nickname: str = Field(min_length=1, max_length=50)
    invite_code: str | None = Field(default=None, max_length=100)


class LoginRequest(StrictModel):
    email: EmailStr
    password: str = Field(max_length=128)


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=1, max_length=200)


class UserOut(StrictModel):
    id: str
    email: str
    nickname: str
    role: str


class SignupResponse(StrictModel):
    user: UserOut


class TokenResponse(StrictModel):
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"  # noqa: S105


class MeResponse(UserOut):
    settings: dict[str, Any]
