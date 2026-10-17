"""Charging logic: one order, one payment, one event.

Idempotency model
-----------------
``payments.order_id`` is unique, so the database is the arbiter: the first
caller inserts, everyone else replays the stored row. The provider call happens
*before* the insert with the order id as the provider idempotency key, so even
a crash between the charge and the commit recharges nothing (with the
deterministic simulated provider the decision is identical either way).

Eventing
--------
``payment.succeeded`` / ``payment.failed`` are appended to the outbox in the
same transaction as the payment row, so no outcome can be lost between the
charge and its announcement.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from common.db import Database
from common.errors import ConflictError, NotFoundError
from common.events import Event
from common.logging import get_logger
from common.outbox import build_event, claim_event, enqueue_event
from common.schemas import Page
from payment_service import metrics
from payment_service import repository as repo
from payment_service.models import FAILED, SUCCEEDED, Payment
from payment_service.provider import SimulatedPaymentProvider
from payment_service.schemas import PaymentRead, PaymentRequest, PaymentSummary
from payment_service.settings import PaymentServiceSettings

logger = get_logger("payment_service.service")

CHARGE_ENDPOINT = "POST /payments"


def to_read(payment: Payment) -> PaymentRead:
    return PaymentRead(
        payment_id=payment.id,
        order_id=payment.order_id,
        order_number=payment.order_number,
        status=payment.status,  # type: ignore[arg-type]
        amount=payment.amount,
        currency=payment.currency,
        provider=payment.provider,
        provider_reference=payment.provider_reference,
        failure_reason=payment.failure_reason,
        customer_email=payment.customer_email,
        description=payment.description,
        source=payment.source,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
    )


def to_summary(payment: Payment) -> PaymentSummary:
    return PaymentSummary(
        payment_id=payment.id,
        order_id=payment.order_id,
        order_number=payment.order_number,
        status=payment.status,  # type: ignore[arg-type]
        amount=payment.amount,
        currency=payment.currency,
        created_at=payment.created_at,
    )


def payment_event(payment: Payment, *, producer: str, topic: str) -> Event:
    """The ``payment.succeeded`` / ``payment.failed`` payload consumed by order-service."""
    outcome = "succeeded" if payment.status == SUCCEEDED else "failed"
    return build_event(
        event_type=f"payment.{outcome}",
        topic=topic,
        key=str(payment.order_id),
        producer=producer,
        data={
            "payment_id": str(payment.id),
            "order_id": str(payment.order_id),
            "order_number": payment.order_number,
            "status": outcome,
            "amount": f"{payment.amount:.2f}",
            "currency": payment.currency,
            "provider": payment.provider,
            "provider_reference": payment.provider_reference,
            "failure_reason": payment.failure_reason,
            "customer_email": payment.customer_email,
        },
    )


class PaymentService:
    def __init__(self, database: Database, provider: SimulatedPaymentProvider, settings: PaymentServiceSettings) -> None:
        self.database = database
        self.provider = provider
        self.settings = settings
        self.max_amount = Decimal(settings.max_amount)

    # ------------------------------------------------------------------ charge

    async def charge(
        self,
        *,
        request: PaymentRequest,
        source: str = "api",
        event: Event | None = None,
        consumer: str | None = None,
    ) -> tuple[PaymentRead, bool]:
        """Charge an order exactly once. Returns ``(payment, created)``.

        ``event``/``consumer`` are supplied by the Kafka consumer: the event id
        is claimed in the same transaction as the payment row, so a redelivered
        ``order.created`` can never produce a second charge.
        """
        amount = request.amount
        if amount > self.max_amount:
            raise ConflictError(
                "Order total exceeds the payment limit",
                code="amount_limit_exceeded",
                details={"amount": f"{amount:.2f}", "max_amount": self.settings.max_amount},
            )

        # Fast path: this order was charged already.
        existing = await self._find_by_order(request.order_id)
        if existing is not None:
            return await self._replay(existing, request, event=event, consumer=consumer)

        authorization = await self.provider.authorize(
            order_id=str(request.order_id),
            order_number=request.order_number,
            amount=amount,
            currency=request.currency,
            customer_email=request.customer_email,
            idempotency_key=str(request.order_id),
        )
        status = SUCCEEDED if authorization.approved else FAILED
        payment_id = uuid.uuid4()
        failure_reason = None if authorization.approved else (authorization.reason or "payment_declined")

        async with self.database.session() as session:
            if event is not None:
                # Dedup marker and charge are atomic: a crash rolls back both.
                await claim_event(session, event=event, consumer=consumer or source)
            payment = await self._insert(
                session,
                payment_id=payment_id,
                request=request,
                status=status,
                provider_reference=authorization.provider_reference,
                failure_reason=failure_reason,
                source=source,
            )
            if payment is None:
                # A concurrent caller charged this order first; replay its row.
                stored = await repo.get_by_order(session, request.order_id)
                assert stored is not None
                return self._replay_sync(to_read(stored), request)

            enqueue_event(session, payment_event(payment, producer=self.settings.service_name, topic=self.settings.payment_events_topic))
            await session.flush()
            result = to_read(payment)

        metrics.payments_total.labels("succeeded" if status == SUCCEEDED else "failed", source).inc()
        metrics.payment_amount_total.labels(result.currency, result.status).inc(float(result.amount))
        logger.info(
            "payment charged",
            extra={
                "payment_id": str(result.payment_id),
                "order_id": str(result.order_id),
                "status": result.status,
                "amount": str(result.amount),
                "currency": result.currency,
                "provider": result.provider,
                "provider_reference": result.provider_reference,
                "failure_reason": result.failure_reason,
                "source": source,
            },
        )
        return result, True

    async def _insert(
        self,
        session: AsyncSession,
        *,
        payment_id: uuid.UUID,
        request: PaymentRequest,
        status: str,
        provider_reference: str | None,
        failure_reason: str | None,
        source: str,
    ) -> Payment | None:
        return await repo.insert_payment(
            session,
            payment_id=payment_id,
            order_id=request.order_id,
            order_number=request.order_number,
            customer_email=request.customer_email,
            description=request.description,
            amount=request.amount,
            currency=request.currency,
            status=status,
            provider=self.provider.name,
            provider_reference=provider_reference,
            failure_reason=failure_reason,
            source=source,
        )

    # ------------------------------------------------------------------ read paths

    async def get_payment(self, payment_id: uuid.UUID) -> PaymentRead:
        async with self.database.read_session() as session:
            payment = await repo.get_payment(session, payment_id)
            if payment is None:
                raise NotFoundError(
                    f"Payment {payment_id} does not exist", code="payment_not_found", details={"payment_id": str(payment_id)}
                )
            return to_read(payment)

    async def get_by_order(self, order_id: uuid.UUID) -> PaymentRead:
        existing = await self._find_by_order(order_id)
        if existing is None:
            raise NotFoundError(
                f"No payment exists for order {order_id}", code="payment_not_found", details={"order_id": str(order_id)}
            )
        return existing

    async def list_payments(
        self, *, filters: repo.PaymentFilters, limit: int, offset: int
    ) -> Page[PaymentSummary]:
        async with self.database.read_session() as session:
            payments, total = await repo.list_payments(session, filters, limit=limit, offset=offset)
            items = [to_summary(payment) for payment in payments]
        return Page.build(items, total=total, limit=limit, offset=offset)

    # ------------------------------------------------------------------ helpers

    async def _find_by_order(self, order_id: uuid.UUID) -> PaymentRead | None:
        """Read the payment as a DTO: the ORM instance dies with its session."""
        async with self.database.read_session() as session:
            payment = await repo.get_by_order(session, order_id)
            return to_read(payment) if payment is not None else None

    async def _replay(
        self, payment: PaymentRead, request: PaymentRequest, *, event: Event | None, consumer: str | None
    ) -> tuple[PaymentRead, bool]:
        if event is not None:
            async with self.database.session() as session:
                await claim_event(session, event=event, consumer=consumer or "unknown")  # raises EventAlreadyProcessed on redelivery
        metrics.idempotent_replays_total.labels(CHARGE_ENDPOINT).inc()
        logger.info(
            "replaying existing payment",
            extra={"order_id": str(request.order_id), "payment_id": str(payment.payment_id), "status": payment.status},
        )
        return self._replay_sync(payment, request)

    def _replay_sync(self, payment: PaymentRead, request: PaymentRequest) -> tuple[PaymentRead, bool]:
        if payment.amount != request.amount or payment.currency != request.currency:
            raise ConflictError(
                "This order was already charged with different terms",
                code="payment_conflict",
                details={
                    "order_id": str(request.order_id),
                    "payment_id": str(payment.payment_id),
                    "charged_amount": f"{payment.amount:.2f}",
                    "charged_currency": payment.currency,
                    "requested_amount": f"{request.amount:.2f}",
                    "requested_currency": request.currency,
                },
            )
        return payment, False


__all__ = ["CHARGE_ENDPOINT", "PaymentService", "payment_event", "to_read", "to_summary"]
