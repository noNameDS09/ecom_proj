"""Kafka consumers owned by order-service.

This closes the saga's second half: ``payment.events`` ->
:meth:`order_service.service.OrderService.apply_payment_result`.

Delivery is at-least-once, so the handler only ever performs idempotent
operations (the stock commit/release is idempotent by order id, the status
transition is guarded by the ``processed_events`` inbox). A duplicate raises
``EventAlreadyProcessed``, which :class:`common.events.EventConsumer` treats as
a success. Failures are retried in-process and then parked in the DLQ.
"""

from __future__ import annotations

import uuid

from common.events import Event, EventHandler
from common.logging import get_logger
from order_service.service import OrderService

logger = get_logger("order_service.consumers")

PAYMENT_SUCCEEDED = "payment.succeeded"
PAYMENT_FAILED = "payment.failed"
PAYMENT_EVENT_TYPES = frozenset({PAYMENT_SUCCEEDED, PAYMENT_FAILED})


def payment_event_handler(orders: OrderService, *, consumer: str) -> EventHandler:
    """Build the ``payment.events`` handler bound to this service's saga core."""

    async def _handle(event: Event) -> None:
        if event.event_type not in PAYMENT_EVENT_TYPES:
            # Forward compatible: this consumer group only reacts to outcomes.
            logger.debug(
                "ignoring unrelated event", extra={"event_type": event.event_type, "event_id": event.event_id}
            )
            return

        data = event.data or {}
        raw_order_id = data.get("order_id")
        if not raw_order_id:
            raise ValueError(f"payment event {event.event_id} is missing order_id")

        occurred = str(data.get("status") or "")
        outcome = occurred if occurred in ("succeeded", "failed") else (
            "succeeded" if event.event_type == PAYMENT_SUCCEEDED else "failed"
        )
        applied = await orders.apply_payment_result(
            event=event,
            consumer=consumer,
            order_id=uuid.UUID(str(raw_order_id)),
            outcome="succeeded" if outcome == "succeeded" else "failed",
            payment_id=str(data["payment_id"]) if data.get("payment_id") else None,
            reason=data.get("failure_reason") or data.get("reason"),
            source="kafka",
        )
        logger.info(
            "payment event consumed",
            extra={
                "order_id": str(raw_order_id),
                "event_type": event.event_type,
                "event_id": event.event_id,
                "outcome": outcome,
                "applied": applied,
            },
        )

    return _handle


__all__ = ["PAYMENT_EVENT_TYPES", "PAYMENT_FAILED", "PAYMENT_SUCCEEDED", "payment_event_handler"]
