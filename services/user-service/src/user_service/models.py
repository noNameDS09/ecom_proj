"""SQLAlchemy models for identity."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from common.db import Base, TimestampMixin, UUIDPrimaryKeyMixin

ACTIVE = "active"
DISABLED = "disabled"


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # MutableList so in-place edits (user.roles.append("admin")) are tracked.
    roles: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(ARRAY(Text)), nullable=False, default=lambda: ["customer"], server_default=text("'{customer}'")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ACTIVE)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user", lazy="raise")

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE and self.deleted_at is None

    def is_locked(self, *, now: datetime | None = None) -> bool:
        return self.locked_until is not None and self.locked_until > (now or datetime.now(self.locked_until.tzinfo))


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(60))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="refresh_tokens", lazy="raise")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(self.expires_at.tzinfo))

    @property
    def is_usable(self) -> bool:
        return not self.is_revoked and not self.is_expired()
