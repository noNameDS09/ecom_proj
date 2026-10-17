"""Data access for products and stock reservations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from product_service.models import Product, StockReservation, StockReservationItem

SORT_COLUMNS = {
    "name": Product.name,
    "-name": Product.name.desc(),
    "price": Product.price,
    "-price": Product.price.desc(),
    "stock": Product.stock,
    "-stock": Product.stock.desc(),
    "created_at": Product.created_at,
    "-created_at": Product.created_at.desc(),
    "updated_at": Product.updated_at,
    "-updated_at": Product.updated_at.desc(),
}
DEFAULT_SORT = "-created_at"


@dataclass(slots=True)
class ProductFilters:
    q: str | None = None
    category: str | None = None
    ids: list[uuid.UUID] = field(default_factory=list)
    active: bool | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    in_stock: bool | None = None
    include_deleted: bool = False
    sort: str = DEFAULT_SORT


def _apply_filters(stmt: Select, filters: ProductFilters) -> Select:
    if not filters.include_deleted:
        stmt = stmt.where(Product.deleted_at.is_(None))
    if filters.ids:
        stmt = stmt.where(Product.id.in_(filters.ids))
    if filters.q:
        pattern = f"%{filters.q}%"
        stmt = stmt.where(or_(Product.name.ilike(pattern), Product.description.ilike(pattern), Product.sku.ilike(pattern)))
    if filters.category:
        stmt = stmt.where(Product.category == filters.category)
    if filters.active is not None:
        stmt = stmt.where(Product.active.is_(filters.active))
    if filters.min_price is not None:
        stmt = stmt.where(Product.price >= filters.min_price)
    if filters.max_price is not None:
        stmt = stmt.where(Product.price <= filters.max_price)
    if filters.in_stock is not None:
        available = Product.stock - Product.reserved
        stmt = stmt.where(available > 0 if filters.in_stock else available <= 0)
    return stmt


async def get_product(session: AsyncSession, product_id: uuid.UUID, *, include_deleted: bool = False) -> Product | None:
    stmt = select(Product).where(Product.id == product_id)
    if not include_deleted:
        stmt = stmt.where(Product.deleted_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_products_by_ids(session: AsyncSession, product_ids: Sequence[uuid.UUID]) -> list[Product]:
    if not product_ids:
        return []
    stmt = select(Product).where(Product.id.in_(list(product_ids)), Product.deleted_at.is_(None)).order_by(Product.sku)
    return list((await session.execute(stmt)).scalars().all())


async def lock_products(session: AsyncSession, product_ids: Sequence[uuid.UUID]) -> list[Product]:
    """``SELECT ... FOR UPDATE`` in a deterministic order (no lock-order deadlocks)."""
    if not product_ids:
        return []
    stmt = (
        select(Product)
        .where(Product.id.in_(list(product_ids)), Product.deleted_at.is_(None))
        .order_by(Product.id)
        .with_for_update()
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_products(
    session: AsyncSession, filters: ProductFilters, *, limit: int, offset: int
) -> tuple[list[Product], int]:
    count_stmt = _apply_filters(select(func.count()).select_from(Product), filters)
    total = int((await session.execute(count_stmt)).scalar_one())

    stmt = _apply_filters(select(Product), filters)
    order_by = SORT_COLUMNS.get(filters.sort, SORT_COLUMNS[DEFAULT_SORT])
    stmt = stmt.order_by(order_by, Product.id).limit(limit).offset(offset)
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


async def list_categories(session: AsyncSession) -> list[tuple[str, int]]:
    """Distinct categories of active products, with how many each contains."""
    stmt = (
        select(Product.category, func.count())
        .where(Product.deleted_at.is_(None), Product.active.is_(True))
        .group_by(Product.category)
        .order_by(Product.category)
    )
    return [(str(row[0]), int(row[1])) for row in (await session.execute(stmt)).all()]


async def sku_exists(session: AsyncSession, sku: str, *, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(func.count()).select_from(Product).where(Product.sku == sku, Product.deleted_at.is_(None))
    if exclude_id is not None:
        stmt = stmt.where(Product.id != exclude_id)
    return int((await session.execute(stmt)).scalar_one()) > 0


async def get_reservation(session: AsyncSession, order_id: uuid.UUID) -> StockReservation | None:
    return (await session.execute(select(StockReservation).where(StockReservation.order_id == order_id))).scalar_one_or_none()


async def lock_reservation(session: AsyncSession, order_id: uuid.UUID) -> StockReservation | None:
    return (
        await session.execute(select(StockReservation).where(StockReservation.order_id == order_id).with_for_update())
    ).scalar_one_or_none()


async def add_reservation(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    items: list[tuple[Product, int]],
) -> StockReservation:
    reservation = StockReservation(id=uuid.uuid4(), order_id=order_id, status="active")
    reservation.items = [
        StockReservationItem(
            id=uuid.uuid4(),
            product_id=product.id,
            sku=product.sku,
            name=product.name,
            quantity=quantity,
            unit_price=product.price,
        )
        for product, quantity in items
    ]
    session.add(reservation)
    return reservation


def release_reservation(reservation: StockReservation, *, now: datetime | None = None) -> None:
    reservation.status = "released"
    reservation.released_at = now or datetime.now(UTC)


def commit_reservation(reservation: StockReservation, *, now: datetime | None = None) -> None:
    reservation.status = "committed"
    reservation.committed_at = now or datetime.now(UTC)


async def give_back_reserved_stock(session: AsyncSession, reservation: StockReservation) -> None:
    """Release phase: only the ``reserved`` counter changes (goods never left)."""
    for item in reservation.items:
        await session.execute(
            update(Product)
            .where(Product.id == item.product_id)
            .values(reserved=Product.reserved - item.quantity, version=Product.version + 1)
        )


async def ship_reserved_stock(session: AsyncSession, reservation: StockReservation) -> None:
    """Commit phase: stock actually leaves the warehouse."""
    for item in reservation.items:
        await session.execute(
            update(Product)
            .where(Product.id == item.product_id)
            .values(
                stock=Product.stock - item.quantity,
                reserved=Product.reserved - item.quantity,
                version=Product.version + 1,
            )
        )
