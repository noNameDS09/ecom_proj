"""Order service request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from common.schemas import APIModel, Money

OrderStatus = Literal["pending", "paid", "payment_failed", "cancelled", "fulfilled"]


class ShippingAddress(APIModel):
    line1: str = Field(min_length=3, max_length=200)
    line2: str = Field(default="", max_length=200)
    city: str = Field(min_length=2, max_length=100)
    postal_code: str = Field(min_length=2, max_length=20)
    country: str = Field(min_length=2, max_length=60, description="ISO-3166 alpha-2 code or country name")


class OrderItemRequest(APIModel):
    """Prices are never accepted from the client: they come from product-service."""

    product_id: uuid.UUID
    quantity: int = Field(gt=0, le=1000)


class CreateOrderRequest(APIModel):
    items: list[OrderItemRequest] = Field(min_length=1, max_length=50)
    shipping_address: ShippingAddress | None = None
    notes: str | None = Field(default=None, max_length=1000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def _no_duplicate_products(self) -> CreateOrderRequest:
        seen = [item.product_id for item in self.items]
        duplicates = {str(pid) for pid in seen if seen.count(pid) > 1}
        if duplicates:
            raise ValueError(f"duplicate product ids in items: {sorted(duplicates)}; merge them into one line")
        return self


class OrderItemRead(APIModel):
    id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    name: str
    quantity: int
    unit_price: Money
    line_total: Money


class OrderRead(APIModel):
    id: uuid.UUID
    order_number: str
    user_id: uuid.UUID
    user_email: str
    status: OrderStatus
    currency: str
    subtotal: Money
    shipping_fee: Money
    total: Money
    item_count: int
    items: list[OrderItemRead]
    shipping_address: ShippingAddress | None = None
    notes: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None


class OrderSummary(APIModel):
    id: uuid.UUID
    order_number: str
    status: OrderStatus
    total: Money
    currency: str
    item_count: int
    created_at: datetime


class StatusHistoryRead(APIModel):
    from_status: str | None
    to_status: str
    reason: str | None
    source: str
    actor: str | None
    occurred_at: datetime


class CancelOrderRequest(APIModel):
    reason: str = Field(default="cancelled by customer", max_length=200)


class CancelOrderResponse(APIModel):
    order: OrderRead
    stock_released: bool
    release_outcome: str


class OrderTimeline(APIModel):
    order_id: uuid.UUID
    order_number: str
    status: OrderStatus
    history: list[StatusHistoryRead]


class PaymentRead(APIModel):
    """Payment attempt as seen by order-service clients."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    status: Literal["succeeded", "failed"]
    amount: Money
    currency: str
    provider: str
    provider_reference: str | None = None
    failure_reason: str | None = None
    created_at: datetime


class PayOrderResponse(APIModel):
    """Result of a synchronous payment attempt.

    ``applied`` tells the caller whether *this* request performed the saga
    transition; a retry of an already-applied payment returns ``false`` and the
    order in its final status.
    """

    order: OrderRead
    payment: PaymentRead
    applied: bool


class PaymentResultRequest(APIModel):
    """Internal/ops entrypoint: apply a payment outcome to the saga.

    Send the same ``event_id`` again to make a retry a no-op (this is exactly
    how the Kafka consumer de-duplicates deliveries).
    """

    outcome: Literal["succeeded", "failed"]
    payment_id: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=200)
    event_id: str | None = Field(default=None, max_length=64, description="Optional: reuse to make the call idempotent")


class PaymentResultResponse(APIModel):
    order: OrderRead
    applied: bool


class PricingPreviewLine(APIModel):
    product_id: uuid.UUID
    sku: str
    name: str
    quantity: int
    unit_price: Money
    line_total: Money
    available: int
    requested: int


class PricingPreview(APIModel):
    """Dry run of order creation: priced basket, availability and totals."""

    currency: str
    lines: list[PricingPreviewLine]
    subtotal: Money
    shipping_fee: Money
    total: Money
    item_count: int
    all_available: bool
    unavailable_product_ids: list[uuid.UUID] = Field(default_factory=list)


__all__ = [
    "CancelOrderRequest",
    "CancelOrderResponse",
    "CreateOrderRequest",
    "OrderItemRead",
    "OrderItemRequest",
    "OrderRead",
    "OrderStatus",
    "OrderSummary",
    "OrderTimeline",
    "PayOrderResponse",
    "PaymentRead",
    "PaymentResultRequest",
    "PaymentResultResponse",
    "PricingPreview",
    "PricingPreviewLine",
    "ShippingAddress",
    "StatusHistoryRead",
]
