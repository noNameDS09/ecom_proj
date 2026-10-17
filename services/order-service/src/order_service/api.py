"""HTTP API for the order service.

Authorization model
-------------------
* order reads/writes : the owner of the order, or ``admin``
* internal endpoints : service token (payment result notifications, ops/replay)

Order creation requires an ``Idempotency-Key`` header: the same key with the
same payload replays the stored response (200 + ``X-Idempotent-Replay: true``),
the same key with a different payload is a 409.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status

from common import context
from common.auth import Principal, current_principal, require_internal_token, require_roles
from common.errors import ValidationError
from common.events import TOPIC_PAYMENT_EVENTS, Event, EventAlreadyProcessed
from common.logging import get_logger
from common.schemas import ErrorResponse, Page, PageParams
from order_service.clients import PaymentServiceClient
from order_service.models import ALL_STATUSES
from order_service.repository import OrderFilters
from order_service.schemas import (
    CancelOrderRequest,
    CancelOrderResponse,
    CreateOrderRequest,
    OrderRead,
    OrderSummary,
    OrderTimeline,
    PaymentRead,
    PaymentResultRequest,
    PaymentResultResponse,
    PayOrderResponse,
    PricingPreview,
)
from order_service.service import INTERNAL_CONSUMER, OrderService

logger = get_logger("order_service.api")

router = APIRouter(prefix="/orders", tags=["orders"])
internal_router = APIRouter(prefix="/internal/orders", tags=["internal"])

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Authentication failed"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
    502: {"model": ErrorResponse, "description": "Dependency answered with an error"},
    503: {"model": ErrorResponse, "description": "Dependency unavailable"},
}


def get_orders(request: Request) -> OrderService:
    return request.app.state.orders


def get_payments(request: Request) -> PaymentServiceClient:
    return request.app.state.payments


Orders = Annotated[OrderService, Depends(get_orders)]
Payments = Annotated[PaymentServiceClient, Depends(get_payments)]

# Fulfilment is an operational step: only staff can move a paid order to fulfilled.
admin_only = require_roles("admin")


# --------------------------------------------------------------------------- create & price


@router.post(
    "",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an order",
    description=(
        "Prices the basket against the catalogue, reserves stock, then persists the order "
        "and `order.created` in a single transaction. The `Idempotency-Key` header is required; "
        "replaying a key returns the original order with `X-Idempotent-Replay: true` and status 200."
    ),
    responses={404: ERROR_RESPONSES[404], 409: ERROR_RESPONSES[409], 422: ERROR_RESPONSES[422], 503: ERROR_RESPONSES[503]},
)
async def create_order(
    payload: CreateOrderRequest,
    orders: Orders,
    response: Response,
    principal: Principal = Depends(current_principal),
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", description="Required. Retries replay the original response.")
    ] = None,
) -> OrderRead:
    order, created = await orders.create_order(principal=principal, payload=payload, idempotency_key=idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/orders/{order.id}"
    response.headers["ETag"] = f'"{order.version}"'
    response.headers["X-Idempotent-Replay"] = "false" if created else "true"
    return order


@router.post(
    "/preview",
    response_model=PricingPreview,
    summary="Price a basket without reserving stock",
    responses={404: ERROR_RESPONSES[404], 422: ERROR_RESPONSES[422], 503: ERROR_RESPONSES[503]},
)
async def preview_order(
    payload: CreateOrderRequest,
    orders: Orders,
    principal: Principal = Depends(current_principal),
) -> PricingPreview:
    return await orders.preview(payload=payload)


# --------------------------------------------------------------------------- reads


@router.get(
    "",
    response_model=Page[OrderSummary],
    summary="List orders",
    description="Customers see their own orders; admins may filter across all users (`user_id`).",
)
async def list_orders(
    orders: Orders,
    params: Annotated[PageParams, Depends(PageParams.as_query)],
    principal: Principal = Depends(current_principal),
    order_status: Annotated[str | None, Query(alias="status", description="Comma separated statuses")] = None,
    user_id: Annotated[uuid.UUID | None, Query(description="Admin only: filter by owner")] = None,
    order_number: Annotated[str | None, Query(max_length=32)] = None,
    created_after: Annotated[datetime | None, Query(description="ISO-8601 timestamp")] = None,
    created_before: Annotated[datetime | None, Query(description="ISO-8601 timestamp")] = None,
    min_total: Annotated[Decimal | None, Query(ge=0)] = None,
) -> Page[OrderSummary]:
    filters = OrderFilters(
        user_id=user_id,
        statuses=_parse_statuses(order_status),
        order_number=order_number,
        created_after=created_after,
        created_before=created_before,
        min_total=min_total,
    )
    return await orders.list_orders(principal=principal, filters=filters, limit=params.limit, offset=params.offset)


@router.get("/{order_id}", response_model=OrderRead, summary="Get an order", responses={404: ERROR_RESPONSES[404]})
async def get_order(
    order_id: Annotated[uuid.UUID, Path()],
    orders: Orders,
    principal: Principal = Depends(current_principal),
) -> OrderRead:
    return await orders.get_order(principal=principal, order_id=order_id)


@router.get(
    "/{order_id}/timeline",
    response_model=OrderTimeline,
    summary="Status history of an order",
    responses={404: ERROR_RESPONSES[404]},
)
async def order_timeline(
    order_id: Annotated[uuid.UUID, Path()],
    orders: Orders,
    principal: Principal = Depends(current_principal),
) -> OrderTimeline:
    return await orders.timeline(principal=principal, order_id=order_id)


# --------------------------------------------------------------------------- lifecycle


@router.post(
    "/{order_id}/cancel",
    response_model=CancelOrderResponse,
    summary="Cancel an order",
    description="Releases the stock reservation (idempotently) and emits `order.cancelled`.",
    responses={404: ERROR_RESPONSES[404], 409: ERROR_RESPONSES[409], 503: ERROR_RESPONSES[503]},
)
async def cancel_order(
    order_id: Annotated[uuid.UUID, Path()],
    orders: Orders,
    payload: CancelOrderRequest | None = None,
    principal: Principal = Depends(current_principal),
) -> CancelOrderResponse:
    body = payload or CancelOrderRequest()
    return await orders.cancel_order(principal=principal, order_id=order_id, reason=body.reason)


@router.post(
    "/{order_id}/fulfil",
    response_model=OrderRead,
    summary="Mark a paid order as fulfilled (admin)",
    description="Operational step: moves `paid` to `fulfilled` and emits `order.fulfilled`. Idempotent for fulfilled orders.",
    responses={403: ERROR_RESPONSES[403], 404: ERROR_RESPONSES[404], 409: ERROR_RESPONSES[409]},
)
async def fulfil_order(
    order_id: Annotated[uuid.UUID, Path()],
    orders: Orders,
    principal: Principal = Depends(admin_only),
) -> OrderRead:
    return await orders.fulfil_order(principal=principal, order_id=order_id)


@router.post(
    "/{order_id}/payments",
    response_model=PayOrderResponse,
    summary="Pay for an order (synchronous)",
    description=(
        "Charges through payment-service (idempotent by order id, so a retry cannot double charge) "
        "and applies the result to the saga. With Kafka enabled, `order.created` triggers the same "
        "payment asynchronously; both paths converge on one idempotent transition."
    ),
    responses={
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
        502: ERROR_RESPONSES[502],
        503: ERROR_RESPONSES[503],
    },
)
async def pay_order(
    order_id: Annotated[uuid.UUID, Path()],
    orders: Orders,
    payments: Payments,
    principal: Principal = Depends(current_principal),
) -> PayOrderResponse:
    order, payment, applied = await orders.pay_order(principal=principal, order_id=order_id, payments=payments)
    return PayOrderResponse(
        order=order,
        payment=PaymentRead(
            payment_id=payment.payment_id,
            order_id=payment.order_id,
            status="succeeded" if payment.outcome == "succeeded" else "failed",
            amount=payment.amount,
            currency=payment.currency,
            provider=payment.provider,
            provider_reference=payment.provider_reference,
            failure_reason=payment.failure_reason,
            created_at=payment.created_at,
        ),
        applied=applied,
    )


# --------------------------------------------------------------------------- internal


@internal_router.post(
    "/{order_id}/payment-result",
    response_model=PaymentResultResponse,
    summary="Apply a payment outcome (internal, idempotent)",
    description=(
        "Used by payment notifications, manual reconciliation and tests to drive the saga "
        "without Kafka. Reuse `event_id` to make the call a no-op on replay."
    ),
    responses={404: ERROR_RESPONSES[404], 409: ERROR_RESPONSES[409]},
)
async def apply_payment_result(
    order_id: Annotated[uuid.UUID, Path()],
    payload: PaymentResultRequest,
    orders: Orders,
    caller: str = Depends(require_internal_token),
) -> PaymentResultResponse:
    event = Event(
        event_type=f"payment.{payload.outcome}",
        data={
            "order_id": str(order_id),
            "payment_id": payload.payment_id,
            "status": payload.outcome,
            "failure_reason": payload.reason,
        },
        topic=TOPIC_PAYMENT_EVENTS,
        key=str(order_id),
        event_id=payload.event_id or context.new_event_id(),
        producer=f"internal:{caller}",
    )
    try:
        applied = await orders.apply_payment_result(
            event=event,
            consumer=INTERNAL_CONSUMER,
            order_id=order_id,
            outcome=payload.outcome,
            payment_id=payload.payment_id,
            reason=payload.reason,
            source="api",
        )
    except EventAlreadyProcessed:
        applied = False
    return PaymentResultResponse(order=await orders.get_order_unscoped(order_id), applied=applied)


def _parse_statuses(raw: str | None) -> list[str]:
    if not raw:
        return []
    values = [chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()]
    invalid = sorted(set(values) - ALL_STATUSES)
    if invalid:
        raise ValidationError(
            f"Unknown order status: {', '.join(invalid)}",
            code="invalid_order_status",
            details={"allowed": sorted(ALL_STATUSES)},
        )
    return values
