"""Product catalogue business logic.

Everything that mutates stock runs inside one database transaction that locks
the affected product rows (``SELECT ... FOR UPDATE``, ordered by id). That is what
makes "never oversell" true even with many concurrent orders across replicas.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from common.cache import TTLCache
from common.db import Database
from common.errors import ConflictError, NotFoundError
from common.logging import get_logger
from common.schemas import Page
from product_service import metrics
from product_service import repository as repo
from product_service.models import Product, StockReservation
from product_service.schemas import (
    CategoryFacet,
    CategoryFacets,
    ProductCreate,
    ProductRead,
    ProductSummary,
    ProductUpdate,
    ReservationItemRead,
    ReservationMutationResult,
    ReservationRead,
    ReservationRequest,
    StockLevel,
)
from product_service.settings import ProductServiceSettings

logger = get_logger("product_service.service")


def to_read(product: Product) -> ProductRead:
    return ProductRead(
        id=product.id,
        sku=product.sku,
        name=product.name,
        description=product.description,
        category=product.category,
        image_url=product.image_url,
        price=product.price,
        currency=product.currency,
        stock=product.stock,
        reserved=product.reserved,
        available=product.available,
        active=product.active,
        version=product.version,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def to_summary(product: Product) -> ProductSummary:
    return ProductSummary(
        id=product.id,
        sku=product.sku,
        name=product.name,
        price=product.price,
        currency=product.currency,
        stock=product.stock,
        available=product.available,
        active=product.active,
    )


def to_reservation_read(reservation: StockReservation) -> ReservationRead:
    currency = "USD"
    return ReservationRead(
        order_id=reservation.order_id,
        status=reservation.status,
        total=reservation.total,
        currency=currency,
        items=[
            ReservationItemRead(
                product_id=item.product_id,
                sku=item.sku,
                name=item.name,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
            for item in reservation.items
        ],
        created_at=reservation.created_at,
        released_at=reservation.released_at,
        committed_at=reservation.committed_at,
    )


class ProductCatalogue:
    def __init__(self, database: Database, settings: ProductServiceSettings) -> None:
        self.database = database
        self.settings = settings
        self.cache: TTLCache[uuid.UUID, ProductRead] = TTLCache(
            maxsize=settings.cache_max_entries, ttl=settings.cache_ttl_seconds
        )

    # ------------------------------------------------------------------ catalogue

    async def create(self, payload: ProductCreate) -> ProductRead:
        async with self.database.session() as session:
            if await repo.sku_exists(session, payload.sku):
                raise ConflictError(
                    f"A product with SKU {payload.sku} already exists",
                    code="sku_already_exists",
                    details={"sku": payload.sku},
                )
            product = Product(
                id=uuid.uuid4(),
                sku=payload.sku,
                name=payload.name,
                description=payload.description,
                category=payload.category,
                image_url=payload.image_url,
                price=payload.price,
                currency=payload.currency or self.settings.default_currency,
                stock=payload.stock,
                reserved=0,
                active=payload.active,
                version=1,
            )
            session.add(product)
            try:
                await session.flush()
            except IntegrityError as exc:  # concurrent insert of the same SKU
                raise ConflictError(
                    f"A product with SKU {payload.sku} already exists",
                    code="sku_already_exists",
                    details={"sku": payload.sku},
                ) from exc
            result = to_read(product)
        metrics.products_created_total.inc()
        logger.info("product created", extra={"product_id": str(result.id), "sku": result.sku, "price": str(result.price)})
        return result

    async def get(self, product_id: uuid.UUID, *, use_cache: bool = True) -> ProductRead:
        if use_cache:
            cached = await self.cache.get(product_id)
            if cached is not None:
                metrics.catalogue_cache_events_total.labels("hit").inc()
                return cached
            metrics.catalogue_cache_events_total.labels("miss").inc()
        async with self.database.read_session() as session:
            product = await repo.get_product(session, product_id)
            if product is None:
                raise NotFoundError(
                    f"Product {product_id} does not exist", code="product_not_found", details={"product_id": str(product_id)}
                )
            result = to_read(product)
        await self.cache.set(product_id, result)
        return result

    async def get_many(self, product_ids: list[uuid.UUID]) -> list[ProductSummary]:
        """Batch lookup used by order-service to price a basket in one round trip."""
        if not product_ids:
            return []
        async with self.database.read_session() as session:
            products = await repo.get_products_by_ids(session, product_ids)
            return [to_summary(product) for product in products]

    async def list(self, filters: repo.ProductFilters, *, limit: int, offset: int) -> Page[ProductRead]:
        async with self.database.read_session() as session:
            products, total = await repo.list_products(session, filters, limit=limit, offset=offset)
            items = [to_read(product) for product in products]
        return Page.build(items, total=total, limit=limit, offset=offset)

    async def categories(self) -> CategoryFacets:
        """Distinct categories of active products (powers the storefront filter chips)."""
        async with self.database.read_session() as session:
            counts = await repo.list_categories(session)
        return CategoryFacets(items=[CategoryFacet(name=name, product_count=count) for name, count in counts])

    async def update(self, product_id: uuid.UUID, payload: ProductUpdate, *, expected_version: int | None = None) -> ProductRead:
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            return await self.get(product_id, use_cache=False)
        async with self.database.session() as session:
            product = await repo.get_product(session, product_id)
            if product is None:
                raise NotFoundError(
                    f"Product {product_id} does not exist", code="product_not_found", details={"product_id": str(product_id)}
                )
            if expected_version is not None and product.version != expected_version:
                raise ConflictError(
                    "Product was modified by another request",
                    code="version_mismatch",
                    details={"expected_version": expected_version, "actual_version": product.version},
                )
            if "stock" in changes:
                new_stock = changes["stock"]
                if new_stock < product.reserved:
                    raise ConflictError(
                        f"Cannot set stock to {new_stock}: {product.reserved} units are reserved for pending orders",
                        code="stock_below_reserved",
                        details={"stock": new_stock, "reserved": product.reserved},
                    )
            for field, value in changes.items():
                setattr(product, field, value)
            product.version += 1
            await session.flush()
            result = to_read(product)
        await self.cache.invalidate(product_id)
        metrics.products_updated_total.inc()
        logger.info("product updated", extra={"product_id": str(product_id), "fields": sorted(changes), "version": result.version})
        return result

    async def delete(self, product_id: uuid.UUID, *, expected_version: int | None = None) -> ProductRead:
        async with self.database.session() as session:
            product = await repo.get_product(session, product_id)
            if product is None:
                raise NotFoundError(
                    f"Product {product_id} does not exist", code="product_not_found", details={"product_id": str(product_id)}
                )
            if expected_version is not None and product.version != expected_version:
                raise ConflictError(
                    "Product was modified by another request",
                    code="version_mismatch",
                    details={"expected_version": expected_version, "actual_version": product.version},
                )
            if product.reserved > 0:
                raise ConflictError(
                    f"Cannot delete: {product.reserved} units are reserved for pending orders",
                    code="product_has_reservations",
                    details={"reserved": product.reserved},
                )
            product.deleted_at = datetime.now(UTC)
            product.active = False
            product.version += 1
            await session.flush()
            result = to_read(product)
        await self.cache.invalidate(product_id)
        metrics.products_deleted_total.inc()
        logger.info("product soft deleted", extra={"product_id": str(product_id), "sku": result.sku})
        return result

    async def stock(self, product_id: uuid.UUID) -> StockLevel:
        product = await self.get(product_id, use_cache=False)
        return StockLevel(
            product_id=product.id,
            sku=product.sku,
            stock=product.stock,
            reserved=product.reserved,
            available=product.available,
            low_stock=product.available <= self.settings.low_stock_threshold,
        )

    # ------------------------------------------------------------------ reservations

    async def reserve(self, payload: ReservationRequest) -> tuple[ReservationRead, bool]:
        """Reserve stock for every line of an order, all-or-nothing.

        ``order_id`` is the natural idempotency key: repeating the exact same
        request returns the original reservation instead of double-booking stock.
        """
        if len(payload.items) > self.settings.max_items_per_reservation:
            raise ConflictError(
                f"At most {self.settings.max_items_per_reservation} distinct items can be reserved per request",
                code="too_many_items",
            )
        requested: dict[uuid.UUID, int] = {}
        for line in payload.items:
            requested[line.product_id] = requested.get(line.product_id, 0) + line.quantity

        async with self.database.session() as session:
            existing = await repo.get_reservation(session, payload.order_id)
            if existing is not None:
                already = {item.product_id: item.quantity for item in existing.items}
                if already == requested and existing.status == "active":
                    metrics.stock_reserved_total.labels("replayed").inc()
                    logger.info(
                        "reservation request replayed",
                        extra={"order_id": str(payload.order_id), "reservation_status": existing.status},
                    )
                    return to_reservation_read(existing), False
                raise ConflictError(
                    "A reservation for this order already exists with different contents",
                    code="reservation_conflict",
                    details={"order_id": str(payload.order_id), "reservation_status": existing.status},
                )

            products = await repo.lock_products(session, list(requested))
            found = {product.id for product in products}
            if missing := requested.keys() - found:
                raise NotFoundError(
                    "One or more products do not exist",
                    code="product_not_found",
                    details={"missing_product_ids": sorted(str(item) for item in missing)},
                )
            unavailable = [product.sku for product in products if not product.active]
            if unavailable:
                raise ConflictError(
                    "One or more products are not available for sale",
                    code="product_not_available",
                    details={"skus": sorted(unavailable)},
                )

            shortage = [
                {"product_id": str(product.id), "sku": product.sku, "requested": requested[product.id], "available": product.available}
                for product in products
                if product.available < requested[product.id]
            ]
            if shortage:
                metrics.stock_reserved_total.labels("insufficient_stock").inc()
                logger.warning(
                    "stock reservation rejected",
                    extra={"order_id": str(payload.order_id), "shortage": shortage},
                )
                raise ConflictError(
                    "Insufficient stock for one or more products",
                    code="insufficient_stock",
                    details={"shortage": shortage},
                )

            for product in products:
                quantity = requested[product.id]
                product.reserved += quantity
                product.version += 1
                metrics.stock_units_changed_total.labels("reserved").inc(quantity)

            reservation = await repo.add_reservation(
                session, order_id=payload.order_id, items=[(product, requested[product.id]) for product in products]
            )
            await session.flush()
            result = to_reservation_read(reservation)
            for product_id in requested:
                await self.cache.invalidate(product_id)

        metrics.stock_reserved_total.labels("reserved").inc()
        logger.info(
            "stock reserved",
            extra={
                "order_id": str(payload.order_id),
                "items": len(result.items),
                "total": str(result.total),
                "products": [str(item.product_id) for item in result.items],
            },
        )
        return result, True

    async def release(self, order_id: uuid.UUID) -> ReservationMutationResult:
        async with self.database.session() as session:
            reservation = await repo.lock_reservation(session, order_id)
            if reservation is None:
                metrics.stock_reservations_released_total.labels("not_found").inc()
                logger.warning("release requested for an unknown reservation", extra={"order_id": str(order_id)})
                return ReservationMutationResult(order_id=order_id, status="not_found")
            if reservation.status == "released":
                metrics.stock_reservations_released_total.labels("already_released").inc()
                return ReservationMutationResult(order_id=order_id, status="already_released")
            if reservation.status == "committed":
                raise ConflictError(
                    "The reservation was already committed; stock has left the warehouse",
                    code="reservation_already_committed",
                    details={"order_id": str(order_id)},
                )
            await repo.give_back_reserved_stock(session, reservation)
            repo.release_reservation(reservation)
            for item in reservation.items:
                metrics.stock_units_changed_total.labels("released").inc(item.quantity)
            await session.flush()
        metrics.stock_reservations_released_total.labels("released").inc()
        logger.info("stock reservation released", extra={"order_id": str(order_id)})
        return ReservationMutationResult(order_id=order_id, status="released", released=True)

    async def commit(self, order_id: uuid.UUID) -> ReservationMutationResult:
        async with self.database.session() as session:
            reservation = await repo.lock_reservation(session, order_id)
            if reservation is None:
                metrics.stock_reservations_committed_total.labels("not_found").inc()
                logger.warning("commit requested for an unknown reservation", extra={"order_id": str(order_id)})
                return ReservationMutationResult(order_id=order_id, status="not_found")
            if reservation.status == "committed":
                metrics.stock_reservations_committed_total.labels("already_committed").inc()
                return ReservationMutationResult(order_id=order_id, status="already_committed", committed=True)
            if reservation.status == "released":
                raise ConflictError(
                    "The reservation was released and cannot be committed",
                    code="reservation_already_released",
                    details={"order_id": str(order_id)},
                )
            await repo.ship_reserved_stock(session, reservation)
            repo.commit_reservation(reservation)
            for item in reservation.items:
                metrics.stock_units_changed_total.labels("committed").inc(item.quantity)
            await session.flush()
        metrics.stock_reservations_committed_total.labels("committed").inc()
        logger.info("stock reservation committed", extra={"order_id": str(order_id)})
        return ReservationMutationResult(order_id=order_id, status="committed", committed=True)

    async def get_reservation(self, order_id: uuid.UUID) -> ReservationRead:
        async with self.database.read_session() as session:
            reservation = await repo.get_reservation(session, order_id)
            if reservation is None:
                raise NotFoundError(
                    f"No reservation exists for order {order_id}",
                    code="reservation_not_found",
                    details={"order_id": str(order_id)},
                )
            return to_reservation_read(reservation)

    def cache_stats(self) -> dict[str, object]:
        return self.cache.stats()


__all__ = ["ProductCatalogue", "to_read", "to_reservation_read", "to_summary"]
