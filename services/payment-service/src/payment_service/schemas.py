"""Payment service request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from common.schemas import APIModel, Money, PositiveMoney

PaymentStatus = Literal["succeeded", "failed"]


class PaymentRequest(APIModel):
    """Charge an order. ``order_id`` is the idempotency key: one order, one payment."""

    order_id: uuid.UUID
    order_number: str = Field(min_length=1, max_length=32)
    amount: PositiveMoney
    currency: str = Field(min_length=3, max_length=3)
    customer_email: str = Field(min_length=3, max_length=320)
    description: str | None = Field(default=None, max_length=200)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class PaymentRead(APIModel):
    payment_id: uuid.UUID
    order_id: uuid.UUID
    order_number: str
    status: PaymentStatus
    amount: Money
    currency: str
    provider: str
    provider_reference: str | None = None
    failure_reason: str | None = None
    customer_email: str
    description: str | None = None
    source: str
    created_at: datetime
    updated_at: datetime


class PaymentSummary(APIModel):
    payment_id: uuid.UUID
    order_id: uuid.UUID
    order_number: str
    status: PaymentStatus
    amount: Money
    currency: str
    created_at: datetime


__all__ = ["PaymentRead", "PaymentRequest", "PaymentStatus", "PaymentSummary"]
