"""Data access for identity."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from user_service.models import RefreshToken, User


@dataclass(slots=True)
class UserFilters:
    q: str | None = None
    role: str | None = None
    status: str | None = None
    ids: list[uuid.UUID] = field(default_factory=list)


def _apply_user_filters(stmt: Select, filters: UserFilters) -> Select:
    stmt = stmt.where(User.deleted_at.is_(None))
    if filters.ids:
        stmt = stmt.where(User.id.in_(filters.ids))
    if filters.q:
        pattern = f"%{filters.q.lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(pattern), func.lower(User.full_name).like(pattern)))
    if filters.role:
        stmt = stmt.where(User.roles.any(filters.role))
    if filters.status:
        stmt = stmt.where(User.status == filters.status)
    return stmt


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))).scalar_one_or_none()


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == email.lower(), User.deleted_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_users(session: AsyncSession, filters: UserFilters, *, limit: int, offset: int) -> tuple[list[User], int]:
    total = int((await session.execute(_apply_user_filters(select(func.count()).select_from(User), filters))).scalar_one())
    stmt = _apply_user_filters(select(User), filters).order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all()), total


async def insert_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    full_name: str,
    roles: Sequence[str],
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email.lower(),
        password_hash=password_hash,
        full_name=full_name,
        roles=list(roles),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def record_login_failure(session: AsyncSession, user: User, *, max_attempts: int, lock_seconds: int) -> bool:
    """Increment the failure counter; returns True when the account just got locked."""
    user.failed_login_attempts += 1
    locked = False
    if user.failed_login_attempts >= max_attempts:
        user.locked_until = datetime.now(UTC) + timedelta(seconds=lock_seconds)
        locked = True
    await session.flush()
    return locked


async def record_login_success(session: AsyncSession, user: User) -> None:
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)
    await session.flush()


# --------------------------------------------------------------------------- refresh tokens


async def insert_refresh_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
    user_agent: str | None,
    ip_address: str | None,
) -> RefreshToken:
    token = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        family_id=family_id,
        token_hash=token_hash,
        expires_at=expires_at,
        user_agent=(user_agent or "")[:300] or None,
        ip_address=(ip_address or "")[:64] or None,
    )
    session.add(token)
    await session.flush()
    return token


async def lock_refresh_token(session: AsyncSession, token_hash: str) -> RefreshToken | None:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def revoke_token(session: AsyncSession, token: RefreshToken, *, reason: str, replaced_by: uuid.UUID | None = None) -> None:
    token.revoked_at = datetime.now(UTC)
    token.revoked_reason = reason
    token.replaced_by = replaced_by
    await session.flush()


async def revoke_family(session: AsyncSession, family_id: uuid.UUID, *, reason: str) -> int:
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
    return int(result.rowcount or 0)


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID, *, reason: str) -> int:
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
    return int(result.rowcount or 0)


async def count_active_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None), RefreshToken.expires_at > func.now()
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_active_sessions_all(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(RefreshToken).where(
        RefreshToken.revoked_at.is_(None), RefreshToken.expires_at > func.now()
    )
    return int((await session.execute(stmt)).scalar_one())


async def delete_expired_tokens(session: AsyncSession, *, older_than_days: int = 7, limit: int = 5000) -> int:
    """Reaper: drop tokens that expired (or were revoked) long ago."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = await session.execute(
        delete(RefreshToken).where(
            RefreshToken.id.in_(select(RefreshToken.id).where(RefreshToken.expires_at < cutoff).limit(limit))
        )
    )
    return int(result.rowcount or 0)
