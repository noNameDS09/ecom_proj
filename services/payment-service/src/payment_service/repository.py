"""Data access for payments."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from payment_service.models import Payment


@dataclass(slots=True)
class PaymentFilters:
    order_id: uuid.UUID | None = None
    statuses: list[str] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    min_amount: Decimal | None = None


def _apply(stmt: Select, filters: PaymentFilters) -> Select:
    if filters.order_id is not None:
        stmt = stmt.where(Payment.order_id == filters.order_id)
    if filters.statuses:
        stmt = stmt.where(Payment.status.in_(filters.statuses))
    if filters.created_after is not None:
        stmt = stmt.where(Payment.created_at >= filters.created_after)
    if filters.created_before is not None:
        stmt = stmt.where(Payment.created_at <= filters.created_before)
    if filters.min_amount is not None:
        stmt = stmt.where(Payment.amount >= filters.min_amount)
    return stmt


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    return (await session.execute(select(Payment).where(Payment.id == payment_id))).scalar_one_or_none()


async def get_by_order(session: AsyncSession, order_id: uuid.UUID, *, for_update: bool = False) -> Payment | None:
    stmt = select(Payment).where(Payment.order_id == order_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_payment(
    session: AsyncSession,
    *,
    payment_id: uuid.UUID,
    order_id: uuid.UUID,
    order_number: str,
    customer_email: str,
    description: str | None,
    amount: Decimal,
    currency: str,
    status: str,
    provider: str,
    provider_reference: str | None,
    failure_reason: str | None,
    source: str,
) -> Payment | None:
    """Insert the payment row, or return ``None`` when the order was charged before.

    ``ON CONFLICT DO NOTHING`` on the unique ``order_id`` makes the database the
    arbiter of idempotency, so even concurrent callers cannot double charge.
    """
    result = await session.execute(
        pg_insert(Payment)
        .values(
            id=payment_id,
            order_id=order_id,
            order_number=order_number,
            customer_email=customer_email,
            description=description,
            amount=amount,
            currency=currency,
            status=status,
            provider=provider,
            provider_reference=provider_reference,
            failure_reason=failure_reason,
            source=source,
        )
        .on_conflict_do_nothing(index_elements=["order_id"])
        .returning(Payment.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    payment = await session.get(Payment, payment_id)
    assert payment is not None
    return payment


async def list_payments(
    session: AsyncSession, filters: PaymentFilters, *, limit: int, offset: int
) -> tuple[list[Payment], int]:
    total = int((await session.execute(_apply(select(func.count()).select_from(Payment), filters))).scalar_one())
    stmt = _apply(select(Payment), filters).order_by(Payment.created_at.desc(), Payment.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all()), total
