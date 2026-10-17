"""The simulated acquirer.

A real integration would call a PSP (Stripe, Adyen, ...) with an idempotency
key and handle webhooks. This module keeps the same *shape* — an async
``authorize`` that returns a terminal decision — but decides deterministically
so tests (and demos) are reproducible:

* ``provider_max_amount``      -> ``amount_limit_exceeded``
* ``provider_decline_cents``   -> ``card_declined`` (e.g. 13 declines 59.13)
* ``provider_decline_emails``  -> ``card_declined`` for specific customers

Everything else is approved and gets a reference derived from the order id, so
even a retried charge returns the same provider reference (the same guarantee a
PSP idempotency key gives).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from decimal import Decimal

from common.logging import get_logger
from payment_service.settings import PaymentServiceSettings

logger = get_logger("payment_service.provider")

PROVIDER_NAME = "simulated"


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    approved: bool
    provider_reference: str | None = None
    reason: str | None = None


class SimulatedPaymentProvider:
    def __init__(self, settings: PaymentServiceSettings) -> None:
        self.settings = settings
        self.name = settings.provider_name

    async def authorize(
        self,
        *,
        order_id: str,
        order_number: str,
        amount: Decimal,
        currency: str,
        customer_email: str,
        idempotency_key: str,
    ) -> AuthorizationResult:
        if self.settings.provider_latency_seconds > 0:
            await asyncio.sleep(self.settings.provider_latency_seconds)

        limit = Decimal(self.settings.provider_max_amount)
        if amount > limit:
            logger.info(
                "provider declined: amount over limit",
                extra={"order_id": order_id, "amount": str(amount), "limit": str(limit)},
            )
            return AuthorizationResult(approved=False, reason="amount_limit_exceeded")

        cents = int((amount * 100).to_integral_value()) % 100
        if cents == self.settings.provider_decline_cents:
            logger.info(
                "provider declined: card test rule",
                extra={"order_id": order_id, "amount": str(amount), "decline_cents": self.settings.provider_decline_cents},
            )
            return AuthorizationResult(approved=False, reason="card_declined")

        if customer_email.lower() in self.settings.declined_emails:
            logger.info("provider declined: customer blocklist", extra={"order_id": order_id, "customer_email": customer_email})
            return AuthorizationResult(approved=False, reason="card_declined")

        digest = hashlib.sha256(f"{order_id}:{idempotency_key}".encode()).hexdigest()[:12]
        reference = f"sim_{digest}"
        logger.info("provider approved", extra={"order_id": order_id, "amount": str(amount), "provider_reference": reference})
        return AuthorizationResult(approved=True, provider_reference=reference)


__all__ = ["PROVIDER_NAME", "AuthorizationResult", "SimulatedPaymentProvider"]
