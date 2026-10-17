"""SQLAlchemy model for payments."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base, TimestampMixin, UUIDPrimaryKeyMixin

SUCCEEDED = "succeeded"
FAILED = "failed"
ALL_STATUSES = frozenset({SUCCEEDED, FAILED})


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "payments"

    order_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True, index=True)
    order_number: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="api")
