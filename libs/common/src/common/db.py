"""Async SQLAlchemy setup shared by every service.

Design notes
------------
* One ``Database`` per process, created in the lifespan and disposed on shutdown.
* ``pool_pre_ping`` + ``pool_recycle`` protect against connections dropped by
  Postgres/network while a service idles.
* ``statement_timeout`` is enforced server side so a runaway query cannot pin a
  connection (and therefore the whole event loop) forever.
* ``Database.session()`` is a small unit of work: commit on success, rollback on
  error, always close.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, Uuid, func, text
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.config import Settings
from common.errors import DependencyUnavailableError
from common.logging import get_logger

logger = get_logger("common.db")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base with a deterministic constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Fetch server generated values (created_at/updated_at defaults, onupdate) with
    # RETURNING during flush. Without this, SQLAlchemy expires those attributes and a
    # later attribute read would try to lazy-load - which is illegal in async code.
    __mapper_args__ = {"eager_defaults": True}

    def to_dict(self) -> dict[str, Any]:
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VersionedMixin:
    """Optimistic concurrency control.

    Services that need it use ``SELECT ... FOR UPDATE`` (see order status
    transitions) or compare the ``version`` column; we deliberately avoid
    SQLAlchemy's mapper-level ``version_id_col`` so a stale write fails with a
    domain error instead of an integration-level commit error.
    """

    version: Mapped[int] = mapped_column(default=1, nullable=False)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_async_engine(
            settings.async_database_url,
            echo=settings.db_echo,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=settings.db_pool_recycle_seconds,
            connect_args={
                "server_settings": {
                    "application_name": settings.service_name,
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "idle_in_transaction_session_timeout": str(max(settings.db_statement_timeout_ms * 4, 60000)),
                },
                "timeout": settings.db_pool_timeout_seconds,
            },
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False, autoflush=False
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Unit of work: commits on clean exit, rolls back on error."""
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """Read-only unit of work (rolls back the implicit transaction on exit)."""
        session = self.session_factory()
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()

    @property
    def session_dependency(self) -> Any:
        """FastAPI dependency yielding a session scoped to the request."""

        async def _dependency() -> AsyncIterator[AsyncSession]:
            async with self.session() as session:
                yield session

        return _dependency

    async def ping(self) -> float:
        """Round-trip a trivial query; returns latency in milliseconds."""
        started = asyncio.get_running_loop().time()
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise DependencyUnavailableError(f"database unavailable: {type(exc).__name__}") from exc
        return round((asyncio.get_running_loop().time() - started) * 1000, 2)

    async def wait_until_ready(self) -> None:
        """Block until Postgres accepts connections (bounded, with backoff)."""
        attempts = max(1, self.settings.db_connect_attempts)
        delay = self.settings.db_connect_retry_delay_seconds
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self.ping()
                if attempt > 1:
                    logger.info("database is accepting connections", extra={"attempt": attempt})
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "database not ready yet",
                    extra={"attempt": attempt, "max_attempts": attempts, "retry_in_seconds": delay, "error": str(exc)},
                )
                await asyncio.sleep(delay)
        raise DependencyUnavailableError(f"database not reachable after {attempts} attempts: {last_error}")

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def create_all(self) -> None:
        """Create tables from metadata - used by tests, never by production startup."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


__all__ = [
    "Base",
    "Database",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "VersionedMixin",
]
