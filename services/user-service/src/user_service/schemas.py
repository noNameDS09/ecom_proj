"""User service request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import EmailStr, Field, field_validator

from common.schemas import APIModel

Password = Annotated[str, Field(min_length=8, max_length=72, description="At least 8 characters, max 72 bytes (bcrypt limit)")]
Role = Literal["customer", "support", "admin"]


class RegisterRequest(APIModel):
    email: EmailStr
    password: Password
    full_name: str = Field(default="", max_length=200)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class LoginRequest(APIModel):
    email: EmailStr
    password: Password

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(APIModel):
    refresh_token: str = Field(min_length=20, max_length=400)


class LogoutRequest(APIModel):
    refresh_token: str | None = Field(default=None, max_length=400, description="Omit to revoke every session of this user")
    all_sessions: bool = Field(default=False, description="Revoke every refresh token of the user")


class PasswordChangeRequest(APIModel):
    current_password: Password
    new_password: Password


class UserUpdate(APIModel):
    full_name: str = Field(min_length=1, max_length=200)


class UserStatusUpdate(APIModel):
    status: Literal["active", "disabled"]
    reason: str | None = Field(default=None, max_length=200)


class UserRead(APIModel):
    id: uuid.UUID
    email: str
    full_name: str
    roles: list[str]
    status: str
    created_at: datetime
    last_login_at: datetime | None = None


class TokenPair(APIModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    roles: list[str] = Field(default_factory=list)


class AuthResponse(APIModel):
    """Login/register/refresh payload: the token pair plus the user profile."""

    tokens: TokenPair
    user: UserRead


class LogoutResponse(APIModel):
    revoked_tokens: int
    all_sessions: bool


class TokenVerificationRequest(APIModel):
    token: str = Field(min_length=20, max_length=2000)


class TokenVerificationResponse(APIModel):
    valid: bool
    user_id: uuid.UUID
    email: str
    roles: list[str]
    expires_at: datetime
    token_id: str | None = None


class PasswordChangeResponse(APIModel):
    status: str = "password_changed"
    revoked_tokens: int


__all__ = [
    "AuthResponse",
    "LoginRequest",
    "LogoutRequest",
    "LogoutResponse",
    "PasswordChangeRequest",
    "PasswordChangeResponse",
    "RefreshRequest",
    "RegisterRequest",
    "TokenPair",
    "TokenVerificationRequest",
    "TokenVerificationResponse",
    "UserRead",
    "UserStatusUpdate",
    "UserUpdate",
]
