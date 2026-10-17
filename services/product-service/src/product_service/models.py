"""SQLAlchemy models for the product catalogue."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from common.db import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Product(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    # Optional absolute URL rendered by the storefront; clients fall back to a
    # generated tile when it is NULL.
    image_url: Mapped[str | None] = mapped_column(String(500))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    # ``stock`` is physical on-hand; ``reserved`` is held for pending orders.
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reservations: Mapped[list[StockReservationItem]] = relationship(back_populates="product", lazy="raise")

    @property
    def available(self) -> int:
        return max(0, self.stock - self.reserved)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class StockReservation(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "stock_reservations"

    order_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[StockReservationItem]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def total(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal("0.00"))


class StockReservationItem(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "stock_reservation_items"

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stock_reservations.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False)
    # Denormalised at reservation time: the ledger keeps what was actually sold
    # even if the catalogue row is renamed afterwards.
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    reservation: Mapped[StockReservation] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="reservations", lazy="raise")

    @property
    def line_total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))
