"""Identity business logic: registration, login, refresh rotation, lockouts."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from common.db import Database
from common.errors import AppError, ConflictError, ForbiddenError, NotFoundError, RateLimitedError, UnauthorizedError
from common.errors import ValidationError as DomainValidationError
from common.logging import get_logger
from common.schemas import Page
from common.security import Principal, decode_access_token, hash_password, issue_access_token, validate_password_strength, verify_password
from user_service import metrics
from user_service import repository as repo
from user_service.models import RefreshToken, User
from user_service.schemas import AuthResponse, PasswordChangeRequest, RegisterRequest, TokenPair, UserRead
from user_service.settings import UserServiceSettings

logger = get_logger("user_service.service")

# Verifying against a real bcrypt hash when the account does not exist keeps the
# response time (and therefore the "does this email exist?" signal) constant.
_DUMMY_PASSWORD = "not-a-real-password-2f8c1d"


def to_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        roles=list(user.roles),
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class LoginResult:
    user: User | None
    tokens: TokenPair | None
    error: AppError | None


class IdentityService:
    def __init__(self, database: Database, settings: UserServiceSettings) -> None:
        self.database = database
        self.settings = settings
        self._dummy_hash = hash_password(_DUMMY_PASSWORD, rounds=settings.bcrypt_rounds)

    # ------------------------------------------------------------------ registration

    async def register(self, payload: RegisterRequest) -> AuthResponse:
        try:
            validate_password_strength(payload.password, min_length=self.settings.password_min_length)
        except ValueError as exc:
            raise DomainValidationError(str(exc), code="weak_password", details={"field": "password"}) from exc

        password_hash = hash_password(payload.password, rounds=self.settings.bcrypt_rounds)
        try:
            async with self.database.session() as session:
                user = await repo.insert_user(
                    session,
                    email=payload.email,
                    password_hash=password_hash,
                    full_name=payload.full_name,
                    roles=[self.settings.default_role],
                )
                user_id = user.id
                tokens = await self._issue_token_pair(session, user, user_agent=None, ip_address=None)
                result = AuthResponse(tokens=tokens, user=to_read(user))
        except IntegrityError as exc:
            metrics.registrations_total.labels("duplicate").inc()
            raise ConflictError(
                "An account with this email already exists",
                code="email_already_registered",
                details={"email": payload.email},
            ) from exc

        metrics.registrations_total.labels("success").inc()
        logger.info("user registered", extra={"user_id": str(user_id), "email": payload.email})
        return result

    # ------------------------------------------------------------------ login

    async def login(self, *, email: str, password: str, user_agent: str | None, ip_address: str | None) -> LoginResult:
        expiry_now = datetime.now(UTC)
        async with self.database.session() as session:
            user = await repo.get_by_email(session, email)
            if user is None:
                verify_password(password, self._dummy_hash)  # constant-time-ish response
                metrics.logins_total.labels("unknown_email").inc()
                return LoginResult(None, None, UnauthorizedError("Invalid email or password", code="invalid_credentials"))

            if not user.is_active:
                metrics.logins_total.labels("disabled").inc()
                return LoginResult(
                    user, None, ForbiddenError("This account has been disabled", code="account_disabled", details={"user_id": str(user.id)})
                )

            if user.is_locked(now=expiry_now):
                retry_after = max(1, int((user.locked_until - expiry_now).total_seconds()))  # type: ignore[operator]
                metrics.logins_total.labels("locked").inc()
                return LoginResult(
                    user,
                    None,
                    RateLimitedError(
                        "Too many failed attempts; the account is temporarily locked",
                        code="account_locked",
                        details={"retry_after_seconds": retry_after},
                        headers={"Retry-After": str(retry_after)},
                    ),
                )

            if not verify_password(password, user.password_hash):
                just_locked = await repo.record_login_failure(
                    session,
                    user,
                    max_attempts=self.settings.max_failed_login_attempts,
                    lock_seconds=self.settings.account_lock_seconds,
                )
                metrics.logins_total.labels("bad_password").inc()
                if just_locked:
                    metrics.account_lockouts_total.inc()
                    logger.warning(
                        "account locked after repeated failed logins",
                        extra={"user_id": str(user.id), "attempts": user.failed_login_attempts},
                    )
                    return LoginResult(
                        user,
                        None,
                        RateLimitedError(
                            "Too many failed attempts; the account is temporarily locked",
                            code="account_locked",
                            details={"retry_after_seconds": self.settings.account_lock_seconds},
                            headers={"Retry-After": str(self.settings.account_lock_seconds)},
                        ),
                    )
                remaining = max(0, self.settings.max_failed_login_attempts - user.failed_login_attempts)
                return LoginResult(
                    user,
                    None,
                    UnauthorizedError(
                        "Invalid email or password",
                        code="invalid_credentials",
                        details={"attempts_remaining": remaining},
                    ),
                )

            await repo.record_login_success(session, user)
            tokens = await self._issue_token_pair(session, user, user_agent=user_agent, ip_address=ip_address)
            result = LoginResult(user, tokens, None)

        metrics.logins_total.labels("success").inc()
        logger.info("login succeeded", extra={"user_id": str(result.user.id), "ip_address": ip_address})  # type: ignore[union-attr]
        return result

    # ------------------------------------------------------------------ refresh / logout

    async def refresh(self, *, refresh_token: str, user_agent: str | None, ip_address: str | None) -> AuthResponse:
        token_hash = hash_refresh_token(refresh_token)
        async with self.database.session() as session:
            stored = await repo.lock_refresh_token(session, token_hash)
            if stored is None:
                metrics.token_operations_total.labels("refresh", "unknown").inc()
                raise UnauthorizedError("Refresh token is not recognised", code="invalid_refresh_token")

            user = await repo.get_by_id(session, stored.user_id)
            if user is None or not user.is_active:
                await self._revoke_family_in_new_transaction(stored.family_id, reason="user_inactive")
                metrics.token_operations_total.labels("refresh", "user_inactive").inc()
                raise ForbiddenError("This account is not allowed to refresh tokens", code="account_disabled")

            if stored.is_revoked:
                # The token was rotated already: someone is replaying a stolen token,
                # so the entire family (all descendants of the original login) dies.
                # Committed in its own transaction: the 401 below must not undo it.
                revoked = await self._revoke_family_in_new_transaction(stored.family_id, reason="reuse_detected")
                metrics.token_operations_total.labels("refresh", "reuse_detected").inc()
                metrics.refresh_reuse_detected_total.inc()
                logger.error(
                    "refresh token reuse detected; revoked the whole session family",
                    extra={"user_id": str(user.id), "family_id": str(stored.family_id), "revoked_tokens": revoked, "ip_address": ip_address},
                )
                raise UnauthorizedError(
                    "This refresh token was already used; all sessions have been revoked",
                    code="refresh_token_reused",
                )

            if stored.is_expired():
                metrics.token_operations_total.labels("refresh", "expired").inc()
                raise UnauthorizedError("Refresh token has expired", code="refresh_token_expired")

            plain_refresh = secrets.token_urlsafe(48)
            replacement = await repo.insert_refresh_token(
                session,
                user_id=user.id,
                family_id=stored.family_id,
                token_hash=hash_refresh_token(plain_refresh),
                expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.refresh_token_ttl_seconds),
                user_agent=user_agent,
                ip_address=ip_address,
            )
            await repo.revoke_token(session, stored, reason="rotated", replaced_by=replacement.id)
            await session.flush()

            access = issue_access_token(
                subject=str(user.id), email=user.email, roles=list(user.roles), settings=self.settings
            )
            tokens = TokenPair(
                access_token=access.token,
                expires_in=access.expires_in,
                refresh_token=plain_refresh,
                refresh_expires_in=self.settings.refresh_token_ttl_seconds,
                roles=list(user.roles),
            )
            result = AuthResponse(tokens=tokens, user=to_read(user))

        metrics.token_operations_total.labels("refresh", "rotated").inc()
        logger.info("refresh token rotated", extra={"user_id": str(result.user.id)})
        return result

    async def logout(self, *, principal: Principal, refresh_token: str | None, all_sessions: bool) -> int:
        async with self.database.session() as session:
            if all_sessions or not refresh_token:
                revoked = await repo.revoke_all_for_user(session, uuid.UUID(principal.user_id), reason="logout_all")
            else:
                stored = await repo.lock_refresh_token(session, hash_refresh_token(refresh_token))
                if stored is None or stored.user_id != uuid.UUID(principal.user_id):
                    # Never leak whether a token exists for another user.
                    revoked = 0
                elif stored.is_revoked:
                    revoked = 0
                else:
                    await repo.revoke_token(session, stored, reason="logout")
                    revoked = 1
        metrics.token_operations_total.labels("logout", "all" if all_sessions else "single").inc()
        logger.info("logout processed", extra={"user_id": principal.user_id, "revoked_tokens": revoked, "all_sessions": all_sessions})
        return revoked

    # ------------------------------------------------------------------ profile / admin

    async def get_user(self, user_id: uuid.UUID) -> UserRead:
        async with self.database.read_session() as session:
            user = await repo.get_by_id(session, user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} does not exist", code="user_not_found", details={"user_id": str(user_id)})
            return to_read(user)

    async def list_users(self, filters: repo.UserFilters, *, limit: int, offset: int) -> Page[UserRead]:
        async with self.database.read_session() as session:
            users, total = await repo.list_users(session, filters, limit=limit, offset=offset)
            items = [to_read(user) for user in users]
        return Page.build(items, total=total, limit=limit, offset=offset)

    async def update_full_name(self, user_id: uuid.UUID, full_name: str) -> UserRead:
        async with self.database.session() as session:
            user = await repo.get_by_id(session, user_id)
            if user is None:
                raise NotFoundError("User does not exist", code="user_not_found")
            user.full_name = full_name
            await session.flush()
            result = to_read(user)
        logger.info("profile updated", extra={"user_id": str(user_id)})
        return result

    async def change_password(self, *, user_id: uuid.UUID, payload: PasswordChangeRequest) -> int:
        try:
            validate_password_strength(payload.new_password, min_length=self.settings.password_min_length)
        except ValueError as exc:
            raise DomainValidationError(str(exc), code="weak_password", details={"field": "new_password"}) from exc
        if payload.current_password == payload.new_password:
            raise DomainValidationError("The new password must be different", code="password_unchanged")

        async with self.database.session() as session:
            user = await repo.get_by_id(session, user_id)
            if user is None:
                raise NotFoundError("User does not exist", code="user_not_found")
            if not verify_password(payload.current_password, user.password_hash):
                metrics.token_operations_total.labels("password_change", "bad_password").inc()
                raise UnauthorizedError("The current password is incorrect", code="invalid_credentials")
            user.password_hash = hash_password(payload.new_password, rounds=self.settings.bcrypt_rounds)
            user.password_changed_at = datetime.now(UTC)
            # Changing the password invalidates every existing session.
            revoked = await repo.revoke_all_for_user(session, user.id, reason="password_changed")
        metrics.token_operations_total.labels("password_change", "success").inc()
        logger.info("password changed", extra={"user_id": str(user_id), "revoked_tokens": revoked})
        return revoked

    async def set_status(self, user_id: uuid.UUID, *, status: str) -> UserRead:
        async with self.database.session() as session:
            user = await repo.get_by_id(session, user_id)
            if user is None:
                raise NotFoundError("User does not exist", code="user_not_found")
            user.status = status
            if status == "disabled":
                await repo.revoke_all_for_user(session, user.id, reason="account_disabled")
            await session.flush()
            result = to_read(user)
        logger.warning("account status changed", extra={"user_id": str(user_id), "status": status})
        return result

    async def verify_access_token(self, token: str) -> Principal:
        """Fallback verification path for services that do not share the JWT secret."""
        principal = decode_access_token(token, self.settings)
        await self.get_user(uuid.UUID(principal.user_id))
        metrics.token_operations_total.labels("verify", "valid").inc()
        return principal

    async def purge_expired_tokens(self) -> int:
        async with self.database.session() as session:
            deleted = await repo.delete_expired_tokens(session)
            active = await repo.count_active_sessions_all(session)
        metrics.active_refresh_tokens_gauge.set(active)
        if deleted:
            logger.info("expired refresh tokens purged", extra={"deleted": deleted, "active_tokens": active})
        return deleted

    # ------------------------------------------------------------------ helpers

    async def _issue_token_pair(
        self,
        session: AsyncSession,
        user: User,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> TokenPair:
        access = issue_access_token(subject=str(user.id), email=user.email, roles=list(user.roles), settings=self.settings)
        plain_refresh = secrets.token_urlsafe(48)
        await repo.insert_refresh_token(
            session,
            user_id=user.id,
            family_id=uuid.uuid4(),
            token_hash=hash_refresh_token(plain_refresh),
            expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.refresh_token_ttl_seconds),
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return TokenPair(
            access_token=access.token,
            expires_in=access.expires_in,
            refresh_token=plain_refresh,
            refresh_expires_in=self.settings.refresh_token_ttl_seconds,
            roles=list(user.roles),
        )

    async def _count_sessions(self, user_id: uuid.UUID) -> int:
        async with self.database.read_session() as session:
            return await repo.count_active_sessions(session, user_id)

    async def _revoke_family_in_new_transaction(self, family_id: uuid.UUID, *, reason: str) -> int:
        """Revoke a token family in its own committed transaction.

        Needed on 401 paths: the request's own session rolls back when we raise, and
        security responses (reuse detection) must survive that rollback.
        """
        async with self.database.session() as session:
            return await repo.revoke_family(session, family_id, reason=reason)

    async def ensure_bootstrap_admin(self) -> None:
        """Create (or update) the demo admin account so a fresh stack is usable."""
        email = self.settings.bootstrap_admin_email.strip().lower()
        if not email or not self.settings.bootstrap_admin_password:
            return
        password_hash = hash_password(self.settings.bootstrap_admin_password, rounds=self.settings.bcrypt_rounds)
        async with self.database.session() as session:
            user = await repo.get_by_email(session, email)
            if user is None:
                await repo.insert_user(
                    session,
                    email=email,
                    password_hash=password_hash,
                    full_name=self.settings.bootstrap_admin_full_name,
                    roles=["admin"],
                )
                logger.info("bootstrap admin account created", extra={"email": email})
                return
            # Keep the account usable across restarts when the password changed in config.
            if "admin" not in user.roles:
                user.roles.append("admin")
            if not verify_password(self.settings.bootstrap_admin_password, user.password_hash):
                user.password_hash = password_hash
                user.password_changed_at = datetime.now(UTC)
                logger.info("bootstrap admin credentials refreshed", extra={"email": email})


__all__ = ["IdentityService", "LoginResult", "RefreshToken", "hash_refresh_token", "to_read"]
