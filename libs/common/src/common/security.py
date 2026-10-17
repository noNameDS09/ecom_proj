"""Password hashing and JWT issuance/verification.

Only user-service issues tokens; every other service verifies them locally with
the shared HS256 secret, so an authenticated request costs no extra network hop.

In a production deployment you would swap HS256 for RS256/EdDSA with a JWKS
endpoint - the verification surface here (``decode_access_token``) is the only
thing that would change.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from common.config import Settings
from common.errors import UnauthorizedError

BCRYPT_MAX_BYTES = 72


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller of the current request."""

    user_id: str
    email: str
    roles: tuple[str, ...] = ()
    token_id: str | None = None
    expires_at: datetime | None = None

    def has_role(self, *roles: str) -> bool:
        return bool(set(roles) & set(self.roles))

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def as_log_context(self) -> dict[str, Any]:
        return {"principal_id": self.user_id, "roles": list(self.roles)}


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    jti: str = field(default_factory=lambda: uuid.uuid4().hex)


# --------------------------------------------------------------------------- passwords


def hash_password(password: str, *, rounds: int = 12) -> str:
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise ValueError(f"password must be at most {BCRYPT_MAX_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Corrupt/legacy hash: treat as a failed login, never a 500.
        return False


def validate_password_strength(password: str, *, min_length: int = 8) -> None:
    """Raise ``ValueError`` with a user-facing reason when the password is too weak."""
    if len(password) < min_length:
        raise ValueError(f"password must be at least {min_length} characters long")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(f"password must be at most {BCRYPT_MAX_BYTES} bytes")
    if password.lower() == password and password.upper() == password:
        raise ValueError("password must contain at least one letter")


# --------------------------------------------------------------------------- tokens


def issue_access_token(
    *,
    subject: str,
    email: str,
    roles: Sequence[str],
    settings: Settings,
    ttl_seconds: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> IssuedToken:
    now = datetime.now(UTC)
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds
    expires_at = now + timedelta(seconds=ttl)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "roles": list(roles),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": jti,
        **(extra_claims or {}),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, expires_in=ttl, expires_at=expires_at, jti=jti)


def decode_access_token(token: str, settings: Settings) -> Principal:
    """Verify signature/claims and return the principal, or raise 401."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError(
            "Access token has expired", code="token_expired", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError(
            "Access token is invalid",
            code="invalid_token",
            details={"reason": str(exc)},
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc

    roles: Iterable[str] = payload.get("roles") or ()
    return Principal(
        user_id=str(payload["sub"]),
        email=str(payload.get("email", "")),
        roles=tuple(str(role) for role in roles),
        token_id=payload.get("jti"),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC) if payload.get("exp") else None,
    )
