"""FastAPI authentication/authorization dependencies."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from common import context
from common.config import Settings
from common.errors import ForbiddenError, UnauthorizedError
from common.security import Principal, decode_access_token

# auto_error=False so *we* control the error envelope instead of starlette's default.
_bearer = HTTPBearer(auto_error=False, description="JWT access token issued by user-service")


def get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:  # pragma: no cover - programming error
        raise RuntimeError("app.state.settings is not configured; build the app with common.app.create_app")
    return settings


async def optional_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Principal | None:
    """Resolve the caller when a valid token is present, otherwise ``None``.

    Used by endpoints that are public but behave differently for signed-in users
    (for example catalogue reads that honour admin visibility flags).
    """
    if credentials is None or not credentials.credentials:
        return None
    principal = decode_access_token(credentials.credentials, settings)
    _bind(principal)
    return principal


async def current_principal(
    principal: Principal | None = Depends(optional_principal),
) -> Principal:
    """Require a valid bearer token."""
    if principal is None:
        raise UnauthorizedError("Authentication required", headers={"WWW-Authenticate": "Bearer"})
    return principal


def require_roles(*roles: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory enforcing that the caller has one of ``roles``."""
    required = tuple(roles)

    async def _dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_role(*required):
            raise ForbiddenError(
                f"Requires one of the following roles: {', '.join(required)}",
                details={"required_roles": list(required), "roles": list(principal.roles)},
            )
        return principal

    return _dependency


def service_identity(service_name: str) -> Callable[[], dict[str, Any]]:
    """Identifies the calling service in logs (used on internal endpoints)."""

    async def _dependency(request: Request) -> dict[str, Any]:
        return {
            "service": request.headers.get("X-Source-Service", "unknown"),
            "caller": service_name,
            "correlation_id": context.get_correlation_id(),
        }

    return _dependency


INTERNAL_TOKEN_HEADER = "X-Internal-Token"


async def require_internal_token(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """Authenticate another *service* (not an end user).

    The asynchronous side of the platform (Kafka consumers, background relays)
    runs without a user token, so internal endpoints use a service credential.
    """
    provided = request.headers.get(INTERNAL_TOKEN_HEADER) or ""
    expected = settings.internal_service_token
    if not expected:
        raise ForbiddenError("Internal endpoints are disabled on this instance", code="internal_endpoints_disabled")
    if not provided or not hmac.compare_digest(provided, expected):
        caller = request.headers.get("X-Source-Service", "unknown")
        raise UnauthorizedError(
            "A valid internal service token is required",
            code="invalid_internal_token",
            details={"caller": caller},
        )
    return request.headers.get("X-Source-Service", "unknown")


async def internal_or_admin(
    principal: Principal | None = Depends(optional_principal),
    request: Request = None,  # type: ignore[assignment]
    settings: Settings = Depends(get_settings),
) -> str:
    """Accept either a service token or an admin user token."""
    provided = request.headers.get(INTERNAL_TOKEN_HEADER) or ""
    if provided and settings.internal_service_token and hmac.compare_digest(provided, settings.internal_service_token):
        return request.headers.get("X-Source-Service", "unknown")
    if principal is not None and principal.has_role("admin", "service"):
        return f"user:{principal.user_id}"
    raise UnauthorizedError(
        "A service token or an admin user token is required", code="invalid_internal_token"
    )


def _bind(principal: Principal) -> None:
    context.set_principal_id(principal.user_id)
    request_id = context.get_request_id()
    if request_id:
        context.set_request_id(request_id)
