"""Payment service settings."""

from __future__ import annotations

from common.config import Settings


class PaymentServiceSettings(Settings):
    service_name: str = "payment-service"
    service_version: str = "0.1.0"
    port: int = 8004
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/payment_db"

    # Simulated acquirer (swap the provider module for a real PSP)
    provider_name: str = "simulated"
    provider_latency_seconds: float = 0.05
    provider_max_amount: str = "5000.00"
    # Amounts whose cents equal this value are declined, e.g. 13 -> 59.13
    provider_decline_cents: int = 13
    # Comma separated list of customers whose cards always fail
    provider_decline_emails: str = ""

    # Domain limits
    max_amount: str = "50000.00"

    # Kafka
    order_events_topic: str = "order.events"
    payment_events_topic: str = "payment.events"
    consumer_group: str = "payment-service.order-events"

    @property
    def declined_emails(self) -> frozenset[str]:
        return frozenset(entry.strip().lower() for entry in self.provider_decline_emails.split(",") if entry.strip())
