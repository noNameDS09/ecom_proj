"""Shared pydantic models: response envelope, money type, pagination."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, computed_field

# Money is exact: stored as NUMERIC in Postgres and serialised as a JSON string
# ("19.99") so clients never see float rounding artefacts.
Money = Annotated[
    Decimal,
    PlainSerializer(lambda value: f"{Decimal(value):.2f}", return_type=str, when_used="json"),
]
PositiveMoney = Annotated[
    Decimal,
    Field(gt=0, max_digits=12, decimal_places=2),
    PlainSerializer(lambda value: f"{Decimal(value):.2f}", return_type=str, when_used="json"),
]

T = TypeVar("T")


class APIModel(BaseModel):
    """Base for request/response models: strict, trimmed, ORM friendly."""

    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=False,
        ser_json_timedelta="float",
    )


class PageMeta(APIModel):
    limit: int
    offset: int
    total: int
    has_more: bool


class Page(APIModel, Generic[T]):
    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], *, total: int, limit: int, offset: int) -> Page[T]:
        return cls(
            items=items,
            meta=PageMeta(limit=limit, offset=offset, total=total, has_more=offset + len(items) < total),
        )


class PageParams(APIModel):
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @classmethod
    def as_query(
        cls,
        limit: int = Query(default=20, ge=1, le=100, description="Max items to return (1-100)"),
        offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    ) -> PageParams:
        return cls(limit=limit, offset=offset)


class ErrorBody(APIModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(APIModel):
    """Documented error envelope; every non-2xx response matches this shape."""

    error: ErrorBody


class HealthStatus(APIModel):
    status: Literal["alive", "ready", "degraded"]
    service: str


class DeleteResponse(APIModel):
    id: str
    deleted: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> str:
        return "deleted" if self.deleted else "unchanged"
