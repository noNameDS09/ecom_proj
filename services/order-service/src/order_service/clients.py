"""Anti-corruption layer for the synchronous dependencies of order-service.

The order service never imports another service's pydantic models: it declares
the slice of the contract it consumes. That keeps the services independently
deployable (extra fields in a response are ignored, missing ones fail loudly)
and translates downstream errors into this service's domain errors.

Resilience lives in :class:`common.httpclient.HttpClient` (timeout, retries with
backoff, circuit breaker, bulkhead). This module only adds the domain mapping.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from common.errors import ConflictError, DependencyUnavailableError, NotFoundError, UpstreamError
from common.events import TOPIC_PAYMENT_EVENTS, Event
from common.httpclient import HttpClient, RetryPolicy, TimeoutBudget
from common.logging import get_logger
from order_service.settings import OrderServiceSettings

logger = get_logger("order_service.clients")


class UpstreamModel(BaseModel):
    """Lenient parsing of a downstream payload (forward compatible)."""

    model_config = ConfigDict(extra="ignore", from_attributes=True, populate_by_name=True)


class ProductView(UpstreamModel):
    id: uuid.UUID
    sku: str
    name: str
    price: Decimal
    currency: str
    stock: int
    available: int
    active: bool


class ReservationItemView(UpstreamModel):
    product_id: uuid.UUID
    sku: str
    name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class ReservationView(UpstreamModel):
    order_id: uuid.UUID
    status: str
    total: Decimal
    currency: str
    items: list[ReservationItemView]


class ProductServiceClient:
    """Typed, resilient client for product-service."""

    def __init__(self, http: HttpClient, settings: OrderServiceSettings) -> None:
        self.http = http
        self.settings = settings
        self.calls = {"lookup": 0, "reserve": 0, "release": 0, "commit": 0}

    # ------------------------------------------------------------------ reads

    async def lookup(self, product_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ProductView]:
        ids = list(dict.fromkeys(product_ids))
        if not ids:
            return {}
        payload = await self.http.post_json("/products/lookup", json={"ids": [str(pid) for pid in ids]})
        products = [ProductView.model_validate(entry) for entry in payload or []]
        self.calls["lookup"] += 1
        return {product.id: product for product in products}

    # ------------------------------------------------------------------ writes

    async def reserve(self, *, order_id: uuid.UUID, items: Sequence[tuple[uuid.UUID, int]]) -> ReservationView:
        """Reserve stock. Idempotent by ``order_id`` so retries are safe.

        Business rejections (shortage, unknown/inactive product) are translated
        into this service's domain errors so the API answers 404/409 instead of
        leaking a 502 that looks like an outage.
        """
        body = {"order_id": str(order_id), "items": [{"product_id": str(pid), "quantity": qty} for pid, qty in items]}
        try:
            response = await self.http.request(
                "POST",
                "/products/reservations",
                json=body,
                expected=(200, 201),
                retry_safe=True,
                headers={"Idempotency-Key": str(order_id)},
            )
        except UpstreamError as exc:
            if exc.details.get("upstream_status") in (404, 409, 422):
                self.raise_for_reservation_failure(exc, order_id=order_id)
            raise
        self.calls["reserve"] += 1
        reservation = ReservationView.model_validate(response.json())
        logger.info(
            "stock reserved",
            extra={
                "order_id": str(order_id),
                "reservation_status": reservation.status,
                "lines": len(reservation.items),
                "total": str(reservation.total),
                "idempotent_replay": response.headers.get("X-Idempotent-Replay") == "true",
            },
        )
        return reservation

    async def release(self, *, order_id: uuid.UUID, reason: str) -> str:
        """Compensating action. Tolerates an unknown reservation (already released)."""
        try:
            payload = await self.http.post_json(
                f"/products/reservations/{order_id}/release",
                json={"reason": reason},
                retry_safe=True,
                expected=(200, 404),
            )
        except ConflictError:
            raise
        except UpstreamError as exc:
            if exc.details.get("upstream_status") == 404:
                return "not_found"
            raise
        self.calls["release"] += 1
        status = (payload or {}).get("status", "unknown")
        logger.info("stock released", extra={"order_id": str(order_id), "release_status": status, "reason": reason})
        return status

    async def commit(self, *, order_id: uuid.UUID) -> str:
        """After a successful payment: turn the hold into an actual stock movement."""
        payload = await self.http.post_json(
            f"/products/reservations/{order_id}/commit", json={}, retry_safe=True, expected=(200, 409, 404)
        )
        self.calls["commit"] += 1
        status = (payload or {}).get("status", "unknown")
        logger.info("stock reservation committed", extra={"order_id": str(order_id), "commit_status": status})
        return status

    # ------------------------------------------------------------------ errors

    @staticmethod
    def raise_for_reservation_failure(error: UpstreamError, *, order_id: uuid.UUID) -> None:
        """Translate a product-service 4xx into an order-service domain error."""
        status = error.details.get("upstream_status")
        code = error.details.get("upstream_code") or "upstream_error"
        catalog: dict[str, type[Exception]] = {
            "insufficient_stock": ConflictError,
            "product_not_available": ConflictError,
            "reservation_conflict": ConflictError,
            "too_many_items": ConflictError,
            "product_not_found": NotFoundError,
        }
        details = {key: value for key, value in error.details.items() if key not in {"dependency", "upstream_status", "upstream_code"}}
        details["order_id"] = str(order_id)
        details["dependency"] = "product-service"
        exc_type = catalog.get(code)
        if exc_type is NotFoundError:
            raise NotFoundError(error.message, code=code, details=details)
        if exc_type is ConflictError:
            raise ConflictError(error.message, code=code, details=details)
        logger.error(
            "unexpected product-service failure while reserving stock",
            extra={"order_id": str(order_id), "upstream_status": status, "upstream_code": code},
        )
        raise error


class PaymentView(UpstreamModel):
    """The slice of payment-service's contract order-service consumes."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    status: str
    amount: Decimal
    currency: str
    provider: str
    provider_reference: str | None = None
    failure_reason: str | None = None
    created_at: datetime

    @property
    def outcome(self) -> str:
        return "succeeded" if self.status == "succeeded" else "failed"


class PaymentServiceClient:
    """Typed, resilient client for payment-service."""

    def __init__(self, http: HttpClient, settings: OrderServiceSettings) -> None:
        self.http = http
        self.settings = settings
        self.calls = {"pay": 0}

    async def pay(
        self,
        *,
        order_id: uuid.UUID,
        order_number: str,
        amount: Decimal,
        currency: str,
        customer_email: str,
    ) -> PaymentView:
        """Charge an order. Idempotent by ``order_id``: a retry cannot double-charge."""
        body = {
            "order_id": str(order_id),
            "order_number": order_number,
            "amount": f"{amount:.2f}",
            "currency": currency,
            "customer_email": customer_email,
            "description": f"Order {order_number}",
        }
        payload = await self.http.post_json(
            "/payments",
            json=body,
            # Safe to retry: payment-service de-duplicates by order id.
            retry_safe=True,
            expected=(200, 201),
        )
        self.calls["pay"] += 1
        payment = PaymentView.model_validate(payload)
        logger.info(
            "payment attempt completed",
            extra={
                "order_id": str(order_id),
                "payment_id": str(payment.payment_id),
                "payment_status": payment.status,
                "amount": str(payment.amount),
                "provider": payment.provider,
            },
        )
        return payment


def payment_result_event(payment: PaymentView, *, producer: str) -> Event:
    """Build the Kafka-shaped event for a *synchronous* payment result.

    The deterministic ``event_id`` means that retrying the synchronous call is
    de-duplicated by exactly the same inbox machinery (``processed_events``)
    that protects the Kafka consumer: one payment, one saga transition.
    """
    outcome = payment.outcome
    return Event(
        event_type=f"payment.{outcome}",
        data={
            "order_id": str(payment.order_id),
            "payment_id": str(payment.payment_id),
            "status": outcome,
            "amount": f"{payment.amount:.2f}",
            "currency": payment.currency,
            "provider": payment.provider,
            "provider_reference": payment.provider_reference,
            "failure_reason": payment.failure_reason,
        },
        topic=TOPIC_PAYMENT_EVENTS,
        key=str(payment.order_id),
        event_id=f"sync-{payment.payment_id}",
        producer=producer,
    )


def build_product_client(settings: OrderServiceSettings) -> tuple[HttpClient, ProductServiceClient]:
    http = HttpClient(
        name="product-service",
        base_url=settings.product_service_url,
        service_name=settings.service_name,
        timeouts=TimeoutBudget(
            connect=settings.upstream_connect_timeout_seconds,
            read=settings.upstream_read_timeout_seconds,
            write=settings.upstream_read_timeout_seconds,
            pool=settings.upstream_connect_timeout_seconds,
        ),
        retry=RetryPolicy(
            attempts=settings.upstream_retry_attempts,
            base_delay=settings.upstream_retry_base_delay_seconds,
        ),
        max_concurrency=settings.upstream_max_concurrency,
        default_headers={"X-Internal-Token": settings.internal_service_token},
        live_path="/live",
    )
    return http, ProductServiceClient(http, settings)


def build_payment_client(settings: OrderServiceSettings) -> tuple[HttpClient, PaymentServiceClient]:
    http = HttpClient(
        name="payment-service",
        base_url=settings.payment_service_url,
        service_name=settings.service_name,
        timeouts=TimeoutBudget(
            connect=settings.upstream_connect_timeout_seconds,
            read=settings.upstream_read_timeout_seconds,
            write=settings.upstream_read_timeout_seconds,
            pool=settings.upstream_connect_timeout_seconds,
        ),
        retry=RetryPolicy(
            attempts=settings.upstream_retry_attempts,
            base_delay=settings.upstream_retry_base_delay_seconds,
        ),
        max_concurrency=settings.upstream_max_concurrency,
        default_headers={"X-Internal-Token": settings.internal_service_token},
        live_path="/live",
    )
    return http, PaymentServiceClient(http, settings)


async def probe(client: ProductServiceClient) -> dict[str, Any]:
    """Readiness probe used by ``/ready``."""
    try:
        await client.http.healthy()
    except DependencyUnavailableError as exc:
        raise DependencyUnavailableError(f"product-service is not ready: {exc.message}", details={"dependency": "product-service"}) from exc
    return {"dependency": "product-service", "circuit": client.http.breaker.state}


def reservation_totals(items: Mapping[uuid.UUID, tuple[ProductView, int]]) -> Decimal:
    return sum((view.price * quantity for view, quantity in items.values()), Decimal("0.00"))


__all__ = [
    "PaymentServiceClient",
    "PaymentView",
    "ProductServiceClient",
    "ProductView",
    "ReservationItemView",
    "ReservationView",
    "UpstreamModel",
    "build_payment_client",
    "build_product_client",
    "payment_result_event",
    "probe",
    "reservation_totals",
]
