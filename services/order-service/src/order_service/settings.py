"""Order service settings."""

from __future__ import annotations

from common.config import Settings


class OrderServiceSettings(Settings):
    service_name: str = "order-service"
    service_version: str = "0.1.0"
    port: int = 8003
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/order_db"

    # Synchronous dependencies
    product_service_url: str = "http://localhost:8001"
    payment_service_url: str = "http://localhost:8004"

    # Resilience budget for outbound calls (timeout -> retries -> circuit breaker)
    upstream_connect_timeout_seconds: float = 2.0
    upstream_read_timeout_seconds: float = 5.0
    upstream_retry_attempts: int = 3
    upstream_retry_base_delay_seconds: float = 0.15
    upstream_breaker_failure_threshold: int = 5
    upstream_breaker_reset_seconds: float = 20.0
    upstream_max_concurrency: int = 32

    # Domain limits
    default_currency: str = "USD"
    max_order_items: int = 50
    max_item_quantity: int = 1000
    order_number_prefix: str = "ORD"
    free_shipping_threshold: str = "100.00"
    shipping_fee: str = "9.99"

    # Idempotency
    idempotency_ttl_seconds: int = 24 * 3600
    idempotency_claim_timeout_seconds: int = 60

    # Kafka
    order_events_topic: str = "order.events"
    payment_events_topic: str = "payment.events"
    consumer_group: str = "order-service.payments"
