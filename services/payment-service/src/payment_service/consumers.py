"""Kafka consumer owned by payment-service.

``order.created`` -> charge the order (idempotent by order id) and let the
outbox announce ``payment.events``. ``order.cancelled`` needs no compensation
here: charges are synchronous, so there is never a *pending* payment to void —
a cancellation simply means order-service ignores any late result.

A redelivered ``order.created`` is a no-op: the event id is claimed in the same
transaction as the payment row (``EventAlreadyProcessed`` marks the duplicate).
"""

from __future__ import annotations

from pydantic import ValidationError

from common.events import Event, EventHandler
from common.logging import get_logger
from payment_service.schemas import PaymentRequest
from payment_service.service import PaymentService

logger = get_logger("payment_service.consumers")

ORDER_CREATED = "order.created"
ORDER_CANCELLED = "order.cancelled"


def order_event_handler(payments: PaymentService, *, consumer: str) -> EventHandler:
    """Build the ``order.events`` handler bound to the charging core."""

    async def _handle(event: Event) -> None:
        if event.event_type == ORDER_CANCELLED:
            logger.info(
                "order cancelled; nothing to void for an unpaid order",
                extra={"order_id": event.data.get("order_id"), "event_id": event.event_id},
            )
            return
        if event.event_type != ORDER_CREATED:
            logger.debug("ignoring unrelated event", extra={"event_type": event.event_type, "event_id": event.event_id})
            return

        data = dict(event.data or {})
        # order.created carries the authoritative total the basket was priced at.
        payload = {
            "order_id": data.get("order_id"),
            "order_number": data.get("order_number"),
            "amount": data.get("total"),
            "currency": data.get("currency"),
            "customer_email": data.get("user_email"),
            "description": f"Order {data.get('order_number')}",
        }
        try:
            request = PaymentRequest.model_validate(payload)
        except ValidationError as exc:
            # Retried by the consumer, then parked in the DLQ: a malformed event
            # is never silently dropped.
            raise ValueError(f"order.created {event.event_id} does not carry a chargeable payload: {exc}") from exc

        payment, created = await payments.charge(request=request, source="kafka", event=event, consumer=consumer)
        logger.info(
            "order.created consumed and charged",
            extra={
                "order_id": str(payment.order_id),
                "payment_id": str(payment.payment_id),
                "payment_status": payment.status,
                "is_new": created,
                "event_id": event.event_id,
            },
        )

    return _handle


__all__ = ["ORDER_CANCELLED", "ORDER_CREATED", "order_event_handler"]
