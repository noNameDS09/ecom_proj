"""User service entrypoint: ``uvicorn user_service.main:app``."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI

from common.app import Runtime, create_app
from common.logging import get_logger
from common.migrations import resolve_migrations_dir
from user_service import __version__
from user_service.api import auth_router, internal_router, users_router
from user_service.service import IdentityService
from user_service.settings import UserServiceSettings

logger = get_logger("user_service")


def build_app(settings: UserServiceSettings | None = None) -> FastAPI:
    settings = settings or UserServiceSettings()
    settings.migrations_dir = resolve_migrations_dir(settings.migrations_dir, package_file=__file__)

    async def bootstrap(app: FastAPI, runtime: Runtime) -> None:
        identity = IdentityService(runtime.db, settings)
        app.state.identity = identity
        await identity.ensure_bootstrap_admin()

        async def session_reaper() -> None:
            """Delete long expired refresh tokens once an hour."""
            while True:
                await asyncio.sleep(3600)
                try:
                    await identity.purge_expired_tokens()
                except Exception as exc:
                    logger.error("refresh token reaper failed", extra={"error": f"{type(exc).__name__}: {exc}"})

        runtime.spawn("refresh-token-reaper", session_reaper, restart_backoff=5.0)
        logger.info(
            "user service bootstrapped",
            extra={
                "access_token_ttl_seconds": settings.access_token_ttl_seconds,
                "refresh_token_ttl_seconds": settings.refresh_token_ttl_seconds,
                "max_failed_login_attempts": settings.max_failed_login_attempts,
                "bootstrap_admin_email": settings.bootstrap_admin_email or "<disabled>",
            },
        )

    return create_app(
        settings=settings,
        name="user-service",
        version=__version__,
        title="User Service API",
        description=(
            "Identity provider for the platform.\n\n"
            "* Registration and login with bcrypt password hashing.\n"
            "* Short lived HS256 access tokens (verified locally by every other service) plus "
            "rotating refresh tokens with reuse detection.\n"
            "* Account lockout after repeated failed logins, admin enable/disable."
        ),
        routers=(auth_router, users_router, internal_router),
        bootstrap=bootstrap,
    )


app = build_app()
