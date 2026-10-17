"""Request/response schemas for the product API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from common.schemas import APIModel, Money, PositiveMoney

SKU_PATTERN = r"^[A-Z0-9][A-Z0-9._-]{2,63}$"
Sku = Annotated[str, Field(min_length=3, max_length=64, pattern=SKU_PATTERN)]
Currency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")]
Category = Annotated[str, Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9 _-]*$")]
ImageUrl = Annotated[str, Field(max_length=500, pattern=r"^https?://\S+$")]


class ProductCreate(APIModel):
    sku: Sku = Field(description="Unique stock keeping unit, e.g. SKU-KEYBOARD-001")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=4000)
    category: Category
    image_url: ImageUrl | None = Field(default=None, description="Absolute http(s) URL rendered by the storefront")
    price: PositiveMoney = Field(description="Unit price, greater than zero")
    currency: Currency = Field(default="USD")
    stock: int = Field(default=0, ge=0, le=1_000_000)
    active: bool = Field(default=True, description="Inactive products stay readable but cannot be ordered")

    @field_validator("sku")
    @classmethod
    def _upper_sku(cls, value: str) -> str:
        return value.upper()

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.upper()


class ProductUpdate(APIModel):
    """PATCH semantics: only the provided fields are changed."""

    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category: Category | None = None
    image_url: ImageUrl | None = None
    price: PositiveMoney | None = None
    currency: Currency | None = None
    stock: int | None = Field(default=None, ge=0, le=1_000_000)
    active: bool | None = None

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class ProductRead(APIModel):
    id: uuid.UUID
    sku: str
    name: str
    description: str
    category: str
    image_url: str | None
    price: Money
    currency: str
    stock: int
    reserved: int
    available: int
    active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ProductSummary(APIModel):
    """Compact projection used by other services (order-service pricing)."""

    id: uuid.UUID
    sku: str
    name: str
    price: Money
    currency: str
    stock: int
    available: int
    active: bool


class CategoryFacet(APIModel):
    """One catalogue category with the number of active products in it."""

    name: str
    product_count: int


class CategoryFacets(APIModel):
    items: list[CategoryFacet]


class ReservationItemRequest(APIModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0, le=10_000)


class ReservationRequest(APIModel):
    order_id: uuid.UUID = Field(description="Order this reservation belongs to; also the idempotency key")
    items: list[ReservationItemRequest] = Field(min_length=1, max_length=100)


class ReservationItemRead(APIModel):
    product_id: uuid.UUID
    sku: str
    name: str
    quantity: int
    unit_price: Money
    line_total: Money


class ReservationRead(APIModel):
    order_id: uuid.UUID
    status: Literal["active", "released", "committed"]
    total: Money
    currency: str
    items: list[ReservationItemRead]
    created_at: datetime
    released_at: datetime | None = None
    committed_at: datetime | None = None


class ReservationMutationResult(APIModel):
    order_id: uuid.UUID
    status: Literal["released", "committed", "already_released", "already_committed", "not_found"]
    released: bool = False
    committed: bool = False


class StockLevel(APIModel):
    product_id: uuid.UUID
    sku: str
    stock: int
    reserved: int
    available: int
    low_stock: bool


__all__ = [
    "CategoryFacet",
    "CategoryFacets",
    "ProductCreate",
    "ProductRead",
    "ProductSummary",
    "ProductUpdate",
    "ReservationItemRead",
    "ReservationItemRequest",
    "ReservationMutationResult",
    "ReservationRead",
    "ReservationRequest",
    "StockLevel",
]
