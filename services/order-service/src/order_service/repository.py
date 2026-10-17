"""Data access for orders."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from order_service.models import Order, OrderItem, OrderStatusHistory


class _UnsetType:
    """Sentinel type for ``from_status``: ``None`` is a valid explicit value."""

    __slots__ = ()


_UNSET = _UnsetType()


@dataclass(slots=True)
class OrderFilters:
    user_id: uuid.UUID | None = None
    statuses: list[str] = field(default_factory=list)
    created_after: datetime | None = None
    created_before: datetime | None = None
    min_total: Decimal | None = None
    order_number: str | None = None


def _apply(stmt: Select, filters: OrderFilters) -> Select:
    if filters.user_id is not None:
        stmt = stmt.where(Order.user_id == filters.user_id)
    if filters.statuses:
        stmt = stmt.where(Order.status.in_(filters.statuses))
    if filters.created_after is not None:
        stmt = stmt.where(Order.created_at >= filters.created_after)
    if filters.created_before is not None:
        stmt = stmt.where(Order.created_at <= filters.created_before)
    if filters.min_total is not None:
        stmt = stmt.where(Order.total >= filters.min_total)
    if filters.order_number:
        stmt = stmt.where(Order.order_number == filters.order_number)
    return stmt


async def get_order(session: AsyncSession, order_id: uuid.UUID, *, for_update: bool = False) -> Order | None:
    stmt = select(Order).where(Order.id == order_id)
    if for_update:
        # Row lock: serialises concurrent status transitions of the same order.
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_order_by_number(session: AsyncSession, order_number: str) -> Order | None:
    return (await session.execute(select(Order).where(Order.order_number == order_number))).scalar_one_or_none()


async def list_orders(
    session: AsyncSession, filters: OrderFilters, *, limit: int, offset: int
) -> tuple[list[Order], int]:
    total = int((await session.execute(_apply(select(func.count()).select_from(Order), filters))).scalar_one())
    stmt = _apply(select(Order), filters).order_by(Order.created_at.desc(), Order.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all()), total


async def history(session: AsyncSession, order_id: uuid.UUID) -> list[OrderStatusHistory]:
    stmt = (
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.occurred_at, OrderStatusHistory.id)
    )
    return list((await session.execute(stmt)).scalars().all())


def build_order(
    *,
    order_id: uuid.UUID,
    order_number: str,
    user_id: uuid.UUID,
    user_email: str,
    currency: str,
    subtotal: Decimal,
    shipping_fee: Decimal,
    total: Decimal,
    shipping_address: dict[str, object] | None,
    notes: str | None,
    idempotency_key: str | None,
    lines: list[tuple[uuid.UUID, str, str, int, Decimal]],
) -> Order:
    order = Order(
        id=order_id,
        order_number=order_number,
        user_id=user_id,
        user_email=user_email,
        status="pending",
        currency=currency,
        subtotal=subtotal,
        shipping_fee=shipping_fee,
        total=total,
        item_count=len(lines),
        shipping_address=shipping_address,
        notes=notes,
        idempotency_key=idempotency_key,
    )
    order.items = [
        OrderItem(
            id=uuid.uuid4(),
            product_id=product_id,
            sku=sku,
            name=name,
            quantity=quantity,
            unit_price=unit_price,
            line_total=(unit_price * quantity).quantize(Decimal("0.01")),
        )
        for product_id, sku, name, quantity, unit_price in lines
    ]
    return order


def append_status(
    session: AsyncSession,
    order: Order,
    *,
    to_status: str,
    reason: str | None = None,
    source: str = "api",
    actor: str | None = None,
    from_status: str | _UnsetType | None = _UNSET,
) -> OrderStatusHistory:
    """Append a status-history row and move the order to ``to_status``.

    ``from_status`` defaults to the order's current status; creation rows pass
    ``None`` explicitly to mean "no previous state".
    """
    entry = OrderStatusHistory(
        order_id=order.id,
        from_status=order.status if from_status is _UNSET else from_status,
        to_status=to_status,
        reason=reason,
        source=source,
        actor=actor,
    )
    session.add(entry)
    order.status = to_status
    order.version += 1
    return entry
