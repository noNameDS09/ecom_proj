"""SQLAlchemy models for orders."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from common.db import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Order lifecycle.
#   pending        -> stock reserved, waiting for the payment result
#   paid           -> payment captured, reservation committed
#   payment_failed -> payment declined, reservation released
#   cancelled      -> cancelled by the customer/admin (reservation released)
#   fulfilled      -> shipped (terminal; set by operations)
PENDING = "pending"
PAID = "paid"
PAYMENT_FAILED = "payment_failed"
CANCELLED = "cancelled"
FULFILLED = "fulfilled"

ALL_STATUSES = frozenset({PENDING, PAID, PAYMENT_FAILED, CANCELLED, FULFILLED})
TERMINAL_STATUSES = frozenset({PAID, CANCELLED, FULFILLED})
CANCELLABLE_STATUSES = frozenset({PENDING, PAYMENT_FAILED})


class Order(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "orders"

    order_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    user_email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=PENDING)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    shipping_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    shipping_address: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin", order_by="OrderItem.sku"
    )

    @property
    def is_cancellable(self) -> bool:
        return self.status in CANCELLABLE_STATUSES


class OrderItem(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="api")
    actor: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
