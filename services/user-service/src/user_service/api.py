"""HTTP API for the user service.

Endpoints
---------
``POST /auth/register``   create an account and return a token pair
``POST /auth/login``      password grant (with lockout after repeated failures)
``POST /auth/refresh``    rotate the refresh token (reuse detection revokes the family)
``POST /auth/logout``     revoke one or every session
``GET  /users/me``        current profile
``PATCH /users/me``       update profile
``POST /users/me/password`` change password (revokes all sessions)
``GET  /users``           admin listing
``GET  /users/{id}``      admin read
``POST /users/{id}/status`` admin enable/disable
``POST /internal/auth/verify`` validate an access token on behalf of another service
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status

from common.auth import Principal, current_principal, require_internal_token, require_roles
from common.errors import UnauthorizedError
from common.logging import get_logger
from common.schemas import ErrorResponse, Page, PageParams
from user_service.repository import UserFilters
from user_service.schemas import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    PasswordChangeRequest,
    PasswordChangeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenVerificationRequest,
    TokenVerificationResponse,
    UserRead,
    UserStatusUpdate,
    UserUpdate,
)
from user_service.service import IdentityService, to_read

logger = get_logger("user_service.api")

auth_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Authentication failed"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}


def get_identity(request: Request) -> IdentityService:
    return request.app.state.identity


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


Identity = Annotated[IdentityService, Depends(get_identity)]
ClientIp = Annotated[str | None, Depends(client_ip)]


# --------------------------------------------------------------------------- auth


@auth_router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    responses={409: ERROR_RESPONSES[409], 422: ERROR_RESPONSES[422]},
)
async def register(payload: RegisterRequest, identity: Identity) -> AuthResponse:
    return await identity.register(payload)


@auth_router.post(
    "/login",
    response_model=AuthResponse,
    summary="Exchange credentials for an access/refresh token pair",
    responses={401: ERROR_RESPONSES[401], 403: ERROR_RESPONSES[403], 429: {"model": ErrorResponse, "description": "Locked"}},
)
async def login(
    payload: LoginRequest,
    identity: Identity,
    ip_address: ClientIp,
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> AuthResponse:
    result = await identity.login(
        email=payload.email,
        password=payload.password,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    if result.error is not None:
        raise result.error
    assert result.tokens is not None and result.user is not None
    return AuthResponse(tokens=result.tokens, user=to_read(result.user))


@auth_router.post(
    "/refresh",
    response_model=AuthResponse,
    summary="Rotate a refresh token",
    responses={401: ERROR_RESPONSES[401], 403: ERROR_RESPONSES[403]},
)
async def refresh(
    payload: RefreshRequest,
    identity: Identity,
    ip_address: ClientIp,
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> AuthResponse:
    return await identity.refresh(
        refresh_token=payload.refresh_token,
        user_agent=user_agent,
        ip_address=ip_address,
    )


@auth_router.post("/logout", response_model=LogoutResponse, summary="Revoke refresh token(s)", responses={401: ERROR_RESPONSES[401]})
async def logout(
    identity: Identity,
    payload: LogoutRequest | None = None,
    principal: Principal = Depends(current_principal),
) -> LogoutResponse:
    body = payload or LogoutRequest()
    revoked = await identity.logout(
        principal=principal,
        refresh_token=body.refresh_token,
        all_sessions=body.all_sessions or body.refresh_token is None,
    )
    return LogoutResponse(revoked_tokens=revoked, all_sessions=body.all_sessions or body.refresh_token is None)


# --------------------------------------------------------------------------- profile


@users_router.get("/me", response_model=UserRead, summary="Current user profile", responses={401: ERROR_RESPONSES[401]})
async def read_me(identity: Identity, principal: Principal = Depends(current_principal)) -> UserRead:
    return await identity.get_user(uuid.UUID(principal.user_id))


@users_router.patch("/me", response_model=UserRead, summary="Update the current profile", responses={401: ERROR_RESPONSES[401]})
async def update_me(payload: UserUpdate, identity: Identity, principal: Principal = Depends(current_principal)) -> UserRead:
    return await identity.update_full_name(uuid.UUID(principal.user_id), payload.full_name)


@users_router.post(
    "/me/password",
    response_model=PasswordChangeResponse,
    summary="Change password (revokes all sessions)",
    responses={401: ERROR_RESPONSES[401], 422: ERROR_RESPONSES[422]},
)
async def change_password(
    payload: PasswordChangeRequest, identity: Identity, principal: Principal = Depends(current_principal)
) -> PasswordChangeResponse:
    revoked = await identity.change_password(user_id=uuid.UUID(principal.user_id), payload=payload)
    return PasswordChangeResponse(revoked_tokens=revoked)


# --------------------------------------------------------------------------- admin


@users_router.get("", response_model=Page[UserRead], summary="List accounts (admin)")
async def list_users(
    identity: Identity,
    params: Annotated[PageParams, Depends(PageParams.as_query)],
    principal: Principal = Depends(require_roles("admin")),
    q: Annotated[str | None, Query(max_length=120, description="Substring match on email or name")] = None,
    role: Annotated[str | None, Query(description="Only users holding this role")] = None,
    user_status: Annotated[str | None, Query(alias="status", description="active | disabled")] = None,
) -> Page[UserRead]:
    return await identity.list_users(
        UserFilters(q=q, role=role, status=user_status), limit=params.limit, offset=params.offset
    )


@users_router.get("/{user_id}", response_model=UserRead, summary="Read an account (admin)", responses={404: ERROR_RESPONSES[404]})
async def read_user(
    user_id: Annotated[uuid.UUID, Path()], identity: Identity, principal: Principal = Depends(require_roles("admin"))
) -> UserRead:
    return await identity.get_user(user_id)


@users_router.post(
    "/{user_id}/status",
    response_model=UserRead,
    summary="Enable/disable an account (admin)",
    responses={404: ERROR_RESPONSES[404]},
)
async def set_user_status(
    user_id: Annotated[uuid.UUID, Path()],
    payload: UserStatusUpdate,
    identity: Identity,
    principal: Principal = Depends(require_roles("admin")),
) -> UserRead:
    if str(user_id) == principal.user_id and payload.status == "disabled":
        raise UnauthorizedError("Administrators cannot disable their own account", code="self_disable_forbidden")
    logger.warning("admin changed account status", extra={"target_user_id": str(user_id), "actor": principal.user_id, "status": payload.status})
    return await identity.set_status(user_id, status=payload.status)


# --------------------------------------------------------------------------- internal


@internal_router.post(
    "/auth/verify",
    response_model=TokenVerificationResponse,
    summary="Verify an access token (internal)",
    description="Alternative to local JWT verification for services that do not share the signing secret.",
    responses={401: ERROR_RESPONSES[401]},
)
async def verify_token(
    payload: TokenVerificationRequest,
    identity: Identity,
    caller: str = Depends(require_internal_token),
) -> TokenVerificationResponse:
    principal = await identity.verify_access_token(payload.token)
    assert principal.expires_at is not None
    return TokenVerificationResponse(
        valid=True,
        user_id=uuid.UUID(principal.user_id),
        email=principal.email,
        roles=list(principal.roles),
        expires_at=principal.expires_at,
        token_id=principal.token_id,
    )
