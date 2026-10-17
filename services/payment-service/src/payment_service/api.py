"""HTTP API for the payment service.

Payment endpoints are *internal*: order-service (or an ops tool with an admin
token) calls them with the shared service token. There is no user-facing
payment route — customers pay through order-service.

``POST /payments`` is idempotent by ``order_id``: the first call charges and
answers 201, every retry replays the stored payment with 200 and
``X-Idempotent-Replay: true``. A declined card is still a *created* payment
(201) whose ``status`` is ``failed``: the HTTP call succeeded, the charge did not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from common.auth import internal_or_admin
from common.errors import ValidationError
from common.logging import get_logger
from common.schemas import ErrorResponse, Page, PageParams
from payment_service.models import ALL_STATUSES
from payment_service.repository import PaymentFilters
from payment_service.schemas import PaymentRead, PaymentRequest, PaymentSummary
from payment_service.service import PaymentService

logger = get_logger("payment_service.api")

router = APIRouter(prefix="/payments", tags=["payments"])

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Authentication failed"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}


def get_payments(request: Request) -> PaymentService:
    return request.app.state.payments


Payments = Annotated[PaymentService, Depends(get_payments)]
InternalCaller = Annotated[str, Depends(internal_or_admin)]


@router.post(
    "",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Charge an order (internal, idempotent by order id)",
    description="A declined card returns 201 with `status: failed`; retries replay the original payment.",
    responses={409: ERROR_RESPONSES[409], 422: ERROR_RESPONSES[422]},
)
async def create_payment(
    payload: PaymentRequest,
    payments: Payments,
    response: Response,
    caller: InternalCaller,
) -> PaymentRead:
    payment, created = await payments.charge(request=payload, source="api")
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["X-Idempotent-Replay"] = "false" if created else "true"
    response.headers["Location"] = f"/payments/{payment.payment_id}"
    logger.info(
        "payment request handled",
        extra={"order_id": str(payload.order_id), "payment_id": str(payment.payment_id), "is_new": created, "caller": caller},
    )
    return payment


@router.get("/{payment_id}", response_model=PaymentRead, summary="Get a payment (internal)", responses={404: ERROR_RESPONSES[404]})
async def get_payment(
    payment_id: Annotated[uuid.UUID, Path()],
    payments: Payments,
    caller: InternalCaller,
) -> PaymentRead:
    return await payments.get_payment(payment_id)


@router.get("", response_model=Page[PaymentSummary], summary="List payments (internal)")
async def list_payments(
    payments: Payments,
    caller: InternalCaller,
    params: Annotated[PageParams, Depends(PageParams.as_query)],
    order_id: Annotated[uuid.UUID | None, Query()] = None,
    payment_status: Annotated[str | None, Query(alias="status", description="Comma separated statuses")] = None,
    created_after: Annotated[datetime | None, Query(description="ISO-8601 timestamp")] = None,
    created_before: Annotated[datetime | None, Query(description="ISO-8601 timestamp")] = None,
    min_amount: Annotated[Decimal | None, Query(ge=0)] = None,
) -> Page[PaymentSummary]:
    filters = PaymentFilters(
        order_id=order_id,
        statuses=_parse_statuses(payment_status),
        created_after=created_after,
        created_before=created_before,
        min_amount=min_amount,
    )
    return await payments.list_payments(filters=filters, limit=params.limit, offset=params.offset)


def _parse_statuses(raw: str | None) -> list[str]:
    if not raw:
        return []
    values = [chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()]
    invalid = sorted(set(values) - ALL_STATUSES)
    if invalid:
        raise ValidationError(
            f"Unknown payment status: {', '.join(invalid)}",
            code="invalid_payment_status",
            details={"allowed": sorted(ALL_STATUSES)},
        )
    return values
