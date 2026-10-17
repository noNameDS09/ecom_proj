"""Order creation and lifecycle: the orchestration core of the saga.

``POST /orders``
  1. claim the ``Idempotency-Key`` (a retried POST must not create a second order)
  2. price the basket against product-service (authoritative prices, never the client's)
  3. reserve stock through product-service (two-phase reservation, idempotent by order id)
  4. write the order, its items, the status history and ``order.created`` in ONE transaction
  5. on a persistence failure, compensate: release the reservation

Payment results (from the synchronous call *or* the Kafka consumer) are applied
through the same idempotent methods, so a duplicated delivery is a no-op.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from common.db import Database
from common.errors import ConflictError, NotFoundError, ValidationError
from common.events import Event, EventAlreadyProcessed
from common.idempotency import IdempotencyClaim, IdempotencyGuard
from common.logging import get_logger
from common.outbox import build_event, claim_event, enqueue_event
from common.schemas import Page
from common.security import Principal
from order_service import metrics
from order_service import repository as repo
from order_service.clients import PaymentServiceClient, PaymentView, ProductServiceClient, ProductView, payment_result_event
from order_service.models import CANCELLED, FULFILLED, PAID, PAYMENT_FAILED, PENDING, Order
from order_service.schemas import (
    CancelOrderResponse,
    CreateOrderRequest,
    OrderItemRead,
    OrderRead,
    OrderSummary,
    OrderTimeline,
    PricingPreview,
    PricingPreviewLine,
    ShippingAddress,
    StatusHistoryRead,
)
from order_service.settings import OrderServiceSettings

logger = get_logger("order_service.service")

CREATE_ENDPOINT = "POST /orders"
PAYMENT_ENDPOINT = "POST /orders/{id}/payments"
CANCELLED_EVENT = "order.cancelled"
CREATED_EVENT = "order.created"
FULFILLED_EVENT = "order.fulfilled"
PAID_EVENT = "order.paid"
# Dedup consumer name for the synchronous payment path (Kafka uses the consumer group).
SYNC_CONSUMER = "order-service.sync"
# Dedup consumer name for the internal/ops payment-result endpoint.
INTERNAL_CONSUMER = "order-service.internal"


# --------------------------------------------------------------------------- converters


def to_read(order: Order) -> OrderRead:
    return OrderRead(
        id=order.id,
        order_number=order.order_number,
        user_id=order.user_id,
        user_email=order.user_email,
        status=order.status,  # type: ignore[arg-type]
        currency=order.currency,
        subtotal=order.subtotal,
        shipping_fee=order.shipping_fee,
        total=order.total,
        item_count=order.item_count,
        items=[
            OrderItemRead(
                id=item.id,
                product_id=item.product_id,
                sku=item.sku,
                name=item.name,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
            for item in order.items
        ],
        shipping_address=ShippingAddress.model_validate(order.shipping_address) if order.shipping_address else None,
        notes=order.notes,
        version=order.version,
        created_at=order.created_at,
        updated_at=order.updated_at,
        paid_at=order.paid_at,
        cancelled_at=order.cancelled_at,
        cancel_reason=order.cancel_reason,
    )


def to_summary(order: Order) -> OrderSummary:
    return OrderSummary(
        id=order.id,
        order_number=order.order_number,
        status=order.status,  # type: ignore[arg-type]
        total=order.total,
        currency=order.currency,
        item_count=order.item_count,
        created_at=order.created_at,
    )


def order_event_data(order: Order) -> dict[str, object]:
    """Payload of ``order.created`` / ``order.cancelled`` (consumed by payment-service)."""
    return {
        "order_id": str(order.id),
        "order_number": order.order_number,
        "user_id": str(order.user_id),
        "user_email": order.user_email,
        "status": order.status,
        "currency": order.currency,
        "subtotal": str(order.subtotal),
        "shipping_fee": str(order.shipping_fee),
        "total": str(order.total),
        "item_count": order.item_count,
        "items": [
            {
                "product_id": str(item.product_id),
                "sku": item.sku,
                "name": item.name,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "line_total": str(item.line_total),
            }
            for item in order.items
        ],
        "created_at": order.created_at.isoformat(),
    }


@dataclass(slots=True)
class PriceBreakdown:
    lines: list[tuple[ProductView, int]]
    subtotal: Decimal
    shipping_fee: Decimal
    total: Decimal
    currency: str


class OrderService:
    def __init__(self, database: Database, products: ProductServiceClient, settings: OrderServiceSettings) -> None:
        self.database = database
        self.products = products
        self.settings = settings
        self.idempotency = IdempotencyGuard(
            database,
            endpoint=CREATE_ENDPOINT,
            ttl_seconds=settings.idempotency_ttl_seconds,
            claim_timeout_seconds=settings.idempotency_claim_timeout_seconds,
        )

    # ------------------------------------------------------------------ create

    async def create_order(
        self, *, principal: Principal, payload: CreateOrderRequest, idempotency_key: str | None
    ) -> tuple[OrderRead, bool]:
        key = IdempotencyGuard.validate_key(idempotency_key)
        fingerprint = IdempotencyGuard.fingerprint(payload.model_dump(mode="json"))
        claim = await self.idempotency.claim(key=key, request_hash=fingerprint, principal_id=principal.user_id)

        if claim.outcome == "replay":
            metrics.idempotent_replays_total.labels(CREATE_ENDPOINT).inc()
            logger.info("replaying stored order response", extra={"idempotency_key": key, "resource_id": claim.resource_id})
            return self._replay(claim), False
        if claim.outcome == "in_flight":
            raise ConflictError(
                "An identical request is still being processed",
                code="request_in_progress",
                details={"idempotency_key": key},
                headers={"Retry-After": "1"},
            )
        if claim.outcome == "key_reuse":
            raise ConflictError(
                "This Idempotency-Key was already used with a different payload",
                code="idempotency_key_reuse",
                details={"idempotency_key": key},
            )

        try:
            order = await self._create(principal=principal, payload=payload, idempotency_key=key)
        except Exception:
            # Free the key so the client may retry after fixing the problem.
            await self.idempotency.release(claim)
            raise

        await self.idempotency.complete(
            claim, status_code=201, body=order.model_dump(mode="json"), resource_id=str(order.id)
        )
        metrics.orders_created_total.labels("created").inc()
        metrics.order_amount_total.labels(order.currency).inc(float(order.total))
        return order, True

    async def _create(self, *, principal: Principal, payload: CreateOrderRequest, idempotency_key: str) -> OrderRead:
        requested: dict[uuid.UUID, int] = {item.product_id: item.quantity for item in payload.items}
        if len(requested) > self.settings.max_order_items:
            raise ValidationError(
                f"An order can contain at most {self.settings.max_order_items} distinct items",
                code="too_many_items",
                details={"max_items": self.settings.max_order_items},
            )
        for product_id, quantity in requested.items():
            if quantity > self.settings.max_item_quantity:
                raise ValidationError(
                    f"Quantity for {product_id} exceeds the per-line limit",
                    code="quantity_limit_exceeded",
                    details={"product_id": str(product_id), "max_quantity": self.settings.max_item_quantity},
                )

        pricing = await self._price(requested)
        if payload.currency and payload.currency != pricing.currency:
            raise ConflictError(
                f"This basket is priced in {pricing.currency}, not {payload.currency}",
                code="currency_mismatch",
                details={"requested": payload.currency, "basket": pricing.currency},
            )

        order_id = uuid.uuid4()
        try:
            await self.products.reserve(order_id=order_id, items=[(product.id, quantity) for product, quantity in pricing.lines])
        except ConflictError as exc:
            # product-service already told us *why* (shortage / unavailable); it is a
            # legitimate business answer, so surface it as 409, not as a 5xx.
            metrics.orders_created_total.labels("rejected").inc()
            logger.warning(
                "order rejected by stock reservation",
                extra={"order_id": str(order_id), "error_code": exc.code, "details": exc.details},
            )
            raise
        except Exception as exc:
            metrics.orders_created_total.labels("dependency_error").inc()
            logger.error("stock reservation failed", extra={"order_id": str(order_id), "error": f"{type(exc).__name__}: {exc}"})
            raise

        try:
            order_read = await self._persist_order(
                order_id=order_id,
                principal=principal,
                pricing=pricing,
                shipping_address=payload.shipping_address.model_dump(mode="json") if payload.shipping_address else None,
                notes=payload.notes,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            # The reservation exists but the order does not: give the stock back.
            logger.error(
                "order persistence failed after reserving stock; compensating",
                extra={"order_id": str(order_id), "error": f"{type(exc).__name__}: {exc}"},
            )
            await self._release_stock(order_id, reason="order_persistence_failed")
            raise
        return order_read

    async def _price(self, requested: dict[uuid.UUID, int]) -> PriceBreakdown:
        try:
            products = await self.products.lookup(list(requested))
        except Exception as exc:
            metrics.orders_created_total.labels("dependency_error").inc()
            logger.error("product lookup failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            raise

        missing = sorted(str(pid) for pid in set(requested) - set(products))
        if missing:
            raise NotFoundError(
                "One or more products do not exist",
                code="product_not_found",
                details={"missing_product_ids": missing},
            )

        inactive = sorted(product.sku for product in products.values() if not product.active)
        if inactive:
            raise ConflictError(
                "One or more products are not available for sale",
                code="product_not_available",
                details={"skus": inactive},
            )

        currencies = {product.currency for product in products.values()}
        if len(currencies) > 1:
            raise ConflictError(
                "All items in an order must use the same currency",
                code="mixed_currencies",
                details={"currencies": sorted(currencies)},
            )

        lines = [(products[pid], quantity) for pid, quantity in requested.items()]
        shortage = [
            {"product_id": str(product.id), "sku": product.sku, "requested": quantity, "available": product.available}
            for product, quantity in lines
            if product.available < quantity
        ]
        if shortage:
            raise ConflictError(
                "Insufficient stock for one or more products",
                code="insufficient_stock",
                details={"shortage": shortage},
            )

        subtotal = sum((product.price * quantity for product, quantity in lines), Decimal("0.00")).quantize(Decimal("0.01"))
        threshold = Decimal(self.settings.free_shipping_threshold)
        shipping_fee = (
            Decimal("0.00") if subtotal >= threshold else Decimal(self.settings.shipping_fee).quantize(Decimal("0.01"))
        )
        return PriceBreakdown(
            lines=lines,
            subtotal=subtotal,
            shipping_fee=shipping_fee,
            total=(subtotal + shipping_fee).quantize(Decimal("0.01")),
            currency=currencies.pop(),
        )

    async def _persist_order(
        self,
        *,
        order_id: uuid.UUID,
        principal: Principal,
        pricing: PriceBreakdown,
        shipping_address: dict[str, object] | None,
        notes: str | None,
        idempotency_key: str,
    ) -> OrderRead:
        order_number = self._order_number()
        async with self.database.session() as session:
            order = repo.build_order(
                order_id=order_id,
                order_number=order_number,
                user_id=uuid.UUID(principal.user_id),
                user_email=principal.email,
                currency=pricing.currency,
                subtotal=pricing.subtotal,
                shipping_fee=pricing.shipping_fee,
                total=pricing.total,
                shipping_address=shipping_address,
                notes=notes,
                idempotency_key=idempotency_key,
                lines=[
                    (product.id, product.sku, product.name, quantity, product.price)
                    for product, quantity in pricing.lines
                ],
            )
            session.add(order)
            await session.flush()  # assign created_at/updated_at from the database
            repo.append_status(
                session,
                order,
                to_status=PENDING,
                reason="order created",
                source="api",
                actor=principal.user_id,
                from_status=None,
            )
            # Same transaction as the order: if this commits, the event will be published.
            enqueue_event(
                session,
                build_event(
                    event_type=CREATED_EVENT,
                    topic=self.settings.order_events_topic,
                    key=str(order.id),
                    producer=self.settings.service_name,
                    data=order_event_data(order),
                ),
            )
            await session.flush()
            result = to_read(order)

        logger.info(
            "order created",
            extra={
                "order_id": str(result.id),
                "order_number": result.order_number,
                "user_id": str(result.user_id),
                "total": str(result.total),
                "items": result.item_count,
            },
        )
        return result

    def _order_number(self) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        return f"{self.settings.order_number_prefix}-{stamp}-{random.randrange(0, 0xFFFFFF):06X}"

    def _replay(self, claim: IdempotencyClaim) -> OrderRead:
        try:
            return OrderRead.model_validate(claim.response_body)
        except Exception as exc:
            logger.error("stored idempotent response is not readable", extra={"resource_id": claim.resource_id, "error": str(exc)})
            raise ConflictError(
                "This request was already answered but the stored response can no longer be replayed",
                code="idempotent_replay_unavailable",
                details={"resource_id": claim.resource_id},
            ) from exc

    # ------------------------------------------------------------------ reads

    async def get_order(self, *, principal: Principal, order_id: uuid.UUID) -> OrderRead:
        async with self.database.read_session() as session:
            order = await repo.get_order(session, order_id)
            if order is None:
                raise NotFoundError(f"Order {order_id} does not exist", code="order_not_found", details={"order_id": str(order_id)})
            self._authorize(principal, order)
            return to_read(order)

    async def get_order_unscoped(self, order_id: uuid.UUID) -> OrderRead:
        """Read without an end-user principal (internal/ops callers only)."""
        async with self.database.read_session() as session:
            order = await repo.get_order(session, order_id)
            if order is None:
                raise NotFoundError(f"Order {order_id} does not exist", code="order_not_found", details={"order_id": str(order_id)})
            return to_read(order)

    async def list_orders(
        self,
        *,
        principal: Principal,
        filters: repo.OrderFilters,
        limit: int,
        offset: int,
    ) -> Page[OrderSummary]:
        if not principal.is_admin:
            filters.user_id = uuid.UUID(principal.user_id)  # customers only ever see their own orders
        async with self.database.read_session() as session:
            orders, total = await repo.list_orders(session, filters, limit=limit, offset=offset)
            items = [to_summary(order) for order in orders]
        return Page.build(items, total=total, limit=limit, offset=offset)

    async def timeline(self, *, principal: Principal, order_id: uuid.UUID) -> OrderTimeline:
        async with self.database.read_session() as session:
            order = await repo.get_order(session, order_id)
            if order is None:
                raise NotFoundError("Order does not exist", code="order_not_found", details={"order_id": str(order_id)})
            self._authorize(principal, order)
            entries = await repo.history(session, order_id)
            return OrderTimeline(
                order_id=order.id,
                order_number=order.order_number,
                status=order.status,  # type: ignore[arg-type]
                history=[
                    StatusHistoryRead(
                        from_status=entry.from_status,
                        to_status=entry.to_status,
                        reason=entry.reason,
                        source=entry.source,
                        actor=entry.actor,
                        occurred_at=entry.occurred_at,
                    )
                    for entry in entries
                ],
            )

    async def preview(self, *, payload: CreateOrderRequest) -> PricingPreview:
        """Dry run: price a basket and check availability without reserving anything."""
        requested = {item.product_id: item.quantity for item in payload.items}
        products = await self.products.lookup(list(requested))
        lines = [
            PricingPreviewLine(
                product_id=product.id,
                sku=product.sku,
                name=product.name,
                quantity=requested[product.id],
                unit_price=product.price,
                line_total=(product.price * requested[product.id]).quantize(Decimal("0.01")),
                available=product.available,
                requested=requested[product.id],
            )
            for product in products.values()
            if product.active
        ]
        subtotal = sum((line.line_total for line in lines), Decimal("0.00")).quantize(Decimal("0.01"))
        threshold = Decimal(self.settings.free_shipping_threshold)
        shipping_fee = Decimal("0.00") if subtotal >= threshold else Decimal(self.settings.shipping_fee).quantize(Decimal("0.01"))
        unavailable = sorted(set(requested) - {line.product_id for line in lines})
        return PricingPreview(
            currency=self.settings.default_currency,
            lines=lines,
            subtotal=subtotal,
            shipping_fee=shipping_fee,
            total=(subtotal + shipping_fee).quantize(Decimal("0.01")),
            item_count=len(lines),
            all_available=bool(lines) and not unavailable and all(line.available >= line.quantity for line in lines),
            unavailable_product_ids=list(unavailable),
        )

    # ------------------------------------------------------------------ cancel

    async def cancel_order(self, *, principal: Principal, order_id: uuid.UUID, reason: str) -> CancelOrderResponse:
        async with self.database.read_session() as session:
            order = await repo.get_order(session, order_id)
            if order is None:
                raise NotFoundError("Order does not exist", code="order_not_found", details={"order_id": str(order_id)})
            self._authorize(principal, order)
            status = order.status

        if status == CANCELLED:
            metrics.orders_cancelled_total.labels("already_cancelled").inc()
            return CancelOrderResponse(order=await self.get_order(principal=principal, order_id=order_id), stock_released=False, release_outcome="already_cancelled")
        if status not in (PENDING, PAYMENT_FAILED):
            raise ConflictError(
                f"An order in status '{status}' cannot be cancelled",
                code="order_not_cancellable",
                details={"status": status},
            )

        # 1. compensate first: the compensation is idempotent, so a crash or retry
        #    between these two steps is safe.
        release_outcome = await self._release_stock(order_id, reason=f"cancelled:{reason}")

        # 2. persist the transition (row lock serialises concurrent cancels)
        async with self.database.session() as session:
            locked = await repo.get_order(session, order_id, for_update=True)
            assert locked is not None
            if locked.status == CANCELLED:
                result = to_read(locked)
            else:
                if locked.status != status:
                    logger.warning(
                        "order status changed while cancelling",
                        extra={"order_id": str(order_id), "expected": status, "actual": locked.status},
                    )
                locked.cancelled_at = datetime.now(UTC)
                locked.cancel_reason = reason
                repo.append_status(session, locked, to_status=CANCELLED, reason=reason, source="api", actor=principal.user_id)
                enqueue_event(
                    session,
                    build_event(
                        event_type=CANCELLED_EVENT,
                        topic=self.settings.order_events_topic,
                        key=str(locked.id),
                        producer=self.settings.service_name,
                        data={**order_event_data(locked), "cancel_reason": reason},
                    ),
                )
                await session.flush()
                result = to_read(locked)

        metrics.orders_cancelled_total.labels("cancelled").inc()
        logger.info("order cancelled", extra={"order_id": str(order_id), "reason": reason, "release_outcome": release_outcome})
        return CancelOrderResponse(order=result, stock_released=release_outcome in {"released", "already_released"}, release_outcome=release_outcome)

    # ------------------------------------------------------------------ fulfilment (operations)

    async def fulfil_order(self, *, principal: Principal, order_id: uuid.UUID) -> OrderRead:
        """Mark a paid order as fulfilled (the terminal operational step).

        Admin-only and idempotent: fulfilling an already-fulfilled order returns
        the current state without appending history or emitting another event.
        """
        async with self.database.read_session() as session:
            order = await repo.get_order(session, order_id)
            if order is None:
                raise NotFoundError("Order does not exist", code="order_not_found", details={"order_id": str(order_id)})
            self._authorize(principal, order)
            status = order.status

        if status == FULFILLED:
            metrics.orders_fulfilled_total.labels("already_fulfilled").inc()
            return await self.get_order(principal=principal, order_id=order_id)
        if status != PAID:
            raise ConflictError(
                f"An order in status '{status}' cannot be fulfilled",
                code="order_not_fulfillable",
                details={"status": status},
            )

        async with self.database.session() as session:
            locked = await repo.get_order(session, order_id, for_update=True)
            assert locked is not None
            if locked.status == FULFILLED:
                result = to_read(locked)
            else:
                if locked.status != PAID:
                    raise ConflictError(
                        f"An order in status '{locked.status}' cannot be fulfilled",
                        code="order_not_fulfillable",
                        details={"status": locked.status},
                    )
                repo.append_status(session, locked, to_status=FULFILLED, source="api", actor=principal.user_id)
                enqueue_event(
                    session,
                    build_event(
                        event_type=FULFILLED_EVENT,
                        topic=self.settings.order_events_topic,
                        key=str(locked.id),
                        producer=self.settings.service_name,
                        data=order_event_data(locked),
                    ),
                )
                await session.flush()
                result = to_read(locked)

        metrics.orders_fulfilled_total.labels("fulfilled").inc()
        logger.info("order fulfilled", extra={"order_id": str(order_id), "actor": principal.user_id})
        return result

    # ------------------------------------------------------------------ payments (synchronous path)

    async def pay_order(
        self, *, principal: Principal, order_id: uuid.UUID, payments: PaymentServiceClient
    ) -> tuple[OrderRead, PaymentView, bool]:
        """Charge the order synchronously and apply the result to the saga.

        The asynchronous path (payment-service consumes ``order.created``) lands
        in :meth:`apply_payment_result` through Kafka instead; both converge on
        the same idempotent transition.
        """
        order = await self.get_order(principal=principal, order_id=order_id)
        if order.status != PENDING:
            raise ConflictError(
                f"An order in status '{order.status}' cannot be paid",
                code="order_not_payable",
                details={"status": order.status},
            )

        logger.info(
            "requesting payment", extra={"order_id": str(order.id), "amount": str(order.total), "currency": order.currency}
        )
        payment = await payments.pay(
            order_id=order.id,
            order_number=order.order_number,
            amount=order.total,
            currency=order.currency,
            customer_email=order.user_email,
        )
        event = payment_result_event(payment, producer=self.settings.service_name)
        try:
            applied = await self.apply_payment_result(
                event=event,
                consumer=SYNC_CONSUMER,
                order_id=order.id,
                outcome="succeeded" if payment.outcome == "succeeded" else "failed",
                payment_id=str(payment.payment_id),
                reason=payment.failure_reason,
                source="api",
            )
        except EventAlreadyProcessed:
            # Concurrent/retried payment call already applied this result.
            applied = False
        updated = await self.get_order(principal=principal, order_id=order_id)
        return updated, payment, applied

    # ------------------------------------------------------------------ payment results

    async def apply_payment_result(
        self,
        *,
        event: Event,
        consumer: str,
        order_id: uuid.UUID,
        outcome: Literal["succeeded", "failed"],
        payment_id: str | None = None,
        reason: str | None = None,
        source: str = "kafka",
    ) -> bool:
        """Apply a payment outcome exactly once (safe under at-least-once delivery).

        Two phases, both idempotent:

        1. read the order and, if a transition is still needed, run the external
           stock commit/release (the side effect that must not be skipped);
        2. in one transaction: claim the event id in ``processed_events`` *and*
           write the new status, its history entry and the follow-up outbox event.

        A crash between the phases simply re-runs phase 1 on redelivery.
        """
        # phase 0: has this event already been applied?
        async with self.database.read_session() as session:
            order = await repo.get_order(session, order_id)
            if order is None:
                logger.error("payment result for an unknown order", extra={"order_id": str(order_id), "event_id": event.event_id})
                raise NotFoundError("Order does not exist", code="order_not_found", details={"order_id": str(order_id)})
            already_final = order.status in (PAID, PAYMENT_FAILED, CANCELLED)
            current_status = order.status

        if already_final:
            logger.info(
                "payment result arrived for an order that is already final",
                extra={"order_id": str(order_id), "status": current_status, "outcome": outcome, "event_id": event.event_id},
            )
            # Still record the event id so a redelivery short-circuits immediately.
            async with self.database.session() as session:
                try:
                    await claim_event(session, event=event, consumer=consumer)
                except EventAlreadyProcessed:
                    return False
            return current_status == (PAID if outcome == "succeeded" else PAYMENT_FAILED)

        # phase 1: external, idempotent side effect. Deliberately outside a DB
        # transaction: an HTTP call must never hold a row lock.
        if outcome == "succeeded":
            stock_outcome = await self.products.commit(order_id=order_id)
        else:
            stock_outcome = await self._release_stock(order_id, reason=f"payment_failed:{reason or 'declined'}")

        # phase 2: durable transition + dedup marker + outbox, atomically
        target = PAID if outcome == "succeeded" else PAYMENT_FAILED
        transitioned = False
        cancelled_race = False
        async with self.database.session() as session:
            await claim_event(session, event=event, consumer=consumer)  # raises EventAlreadyProcessed on redelivery
            locked = await repo.get_order(session, order_id, for_update=True)
            assert locked is not None
            if locked.status == target:
                pass  # a concurrent delivery applied it already; the claim above is the only write
            elif locked.status == CANCELLED:
                cancelled_race = True
                logger.warning(
                    "payment result for a cancelled order; compensating",
                    extra={"order_id": str(order_id), "outcome": outcome, "payment_id": payment_id},
                )
            else:
                if target == PAID:
                    locked.paid_at = datetime.now(UTC)
                repo.append_status(
                    session,
                    locked,
                    to_status=target,
                    reason=reason or (payment_id and f"payment {payment_id}") or None,
                    source=source,
                    actor=f"payment:{payment_id}" if payment_id else "payment-service",
                )
                follow_up = PAID_EVENT if target == PAID else CANCELLED_EVENT
                enqueue_event(
                    session,
                    build_event(
                        event_type=follow_up,
                        topic=self.settings.order_events_topic,
                        key=str(locked.id),
                        producer=self.settings.service_name,
                        data={
                            **order_event_data(locked),
                            "payment_id": payment_id,
                            "payment_status": outcome,
                            "stock_outcome": stock_outcome,
                            "reason": reason,
                        },
                    ),
                )
                await session.flush()
                transitioned = True

        if cancelled_race:
            if outcome == "succeeded":
                # The cancel path already released the stock, so this is a no-op;
                # running it anyway means a crash between reserve and cancel can
                # never leave a committed hold behind a cancelled order.
                await self._release_stock(order_id, reason="late_payment_after_cancel")
            return False

        if not transitioned:
            return False

        metrics.saga_outcomes_total.labels(outcome).inc()
        metrics.orders_status_changes_total.labels(current_status, target, source).inc()
        logger.info(
            "payment result applied",
            extra={
                "order_id": str(order_id),
                "outcome": outcome,
                "status": target,
                "payment_id": payment_id,
                "stock_outcome": stock_outcome,
                "event_id": event.event_id,
            },
        )
        return True

    async def _release_stock(self, order_id: uuid.UUID, *, reason: str) -> str:
        try:
            outcome = await self.products.release(order_id=order_id, reason=reason)
        except Exception as exc:
            metrics.compensations_total.labels(reason.split(":")[0], "failure").inc()
            logger.error(
                "compensating stock release failed",
                extra={"order_id": str(order_id), "reason": reason, "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        metrics.compensations_total.labels(reason.split(":")[0], outcome).inc()
        return outcome

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _authorize(principal: Principal, order: Order) -> None:
        if principal.is_admin:
            return
        if str(order.user_id) != principal.user_id:
            # 404 rather than 403: do not disclose that the order exists.
            raise NotFoundError("Order does not exist", code="order_not_found", details={"order_id": str(order.id)})


__all__ = [
    "CANCELLED_EVENT",
    "CREATED_EVENT",
    "CREATE_ENDPOINT",
    "FULFILLED_EVENT",
    "INTERNAL_CONSUMER",
    "PAID_EVENT",
    "PAYMENT_ENDPOINT",
    "SYNC_CONSUMER",
    "OrderService",
    "PriceBreakdown",
    "order_event_data",
    "to_read",
    "to_summary",
]
