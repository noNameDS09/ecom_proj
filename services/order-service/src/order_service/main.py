"""Order service entrypoint.

Run with ``uvicorn order_service.main:app`` (Docker) or build the ASGI app
explicitly in tests/scripts via :func:`build_app`.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI

from common import health
from common.app import Runtime, create_app
from common.events import EventConsumer, KafkaEventPublisher, NullEventPublisher
from common.logging import get_logger
from common.migrations import resolve_migrations_dir
from common.outbox import OutboxRelay, make_dlq_handler
from order_service import __version__
from order_service.api import internal_router, router
from order_service.clients import build_payment_client, build_product_client
from order_service.clients import probe as probe_products
from order_service.consumers import payment_event_handler
from order_service.service import OrderService
from order_service.settings import OrderServiceSettings

logger = get_logger("order_service")


def build_app(settings: OrderServiceSettings | None = None) -> FastAPI:
    settings = settings or OrderServiceSettings()
    settings.migrations_dir = resolve_migrations_dir(settings.migrations_dir, package_file=__file__)

    async def bootstrap(app: FastAPI, runtime: Runtime) -> None:
        # -- synchronous dependencies (resilient clients, one per target)
        product_http, products = build_product_client(settings)
        await runtime.add_client(product_http)
        payment_http, payments = build_payment_client(settings)
        await runtime.add_client(payment_http)

        orders = OrderService(runtime.db, products, settings)
        app.state.orders = orders
        app.state.payments = payments

        # -- eventing: outbox relay publishes order.events
        publisher = (
            KafkaEventPublisher(settings=settings.kafka, service_name=settings.service_name)
            if settings.kafka.enabled
            else NullEventPublisher(settings.service_name)
        )
        await runtime.set_publisher(publisher)

        relay = OutboxRelay(
            database=runtime.db,
            publisher=publisher,
            service_name=settings.service_name,
            poll_interval=settings.kafka.outbox_poll_interval_seconds,
            batch_size=settings.kafka.outbox_batch_size,
            max_attempts=settings.kafka.outbox_max_attempts,
            backoff_base=settings.kafka.outbox_backoff_base_seconds,
            backoff_max=settings.kafka.outbox_backoff_max_seconds,
        )
        runtime.spawn("outbox-relay", relay.run)
        app.state.outbox = relay

        @app.get("/_outbox", include_in_schema=False)
        async def outbox_status() -> dict[str, object]:
            return {"stats": asdict(relay.stats), "backlog": await relay.backlog()}

        # -- eventing: consume payment results and finish the saga
        if settings.kafka.enabled:
            consumer = EventConsumer(
                settings=settings.kafka,
                service_name=settings.service_name,
                group_id=settings.consumer_group,
                topics=[settings.payment_events_topic],
                handler=payment_event_handler(orders, consumer=settings.consumer_group),
                on_dlq=make_dlq_handler(
                    database=runtime.db,
                    publisher=publisher,
                    service_name=settings.service_name,
                    dlq_topic=settings.kafka.dlq_topic,
                ),
            )
            await runtime.start_consumer(consumer, name="payment-events")
            health.register_readiness(app, "kafka", publisher.healthy)

        # -- readiness: order intake is impossible without product-service
        health.register_readiness(app, "product-service", lambda: probe_products(products))

        logger.info(
            "order service bootstrapped",
            extra={
                "product_service_url": settings.product_service_url,
                "payment_service_url": settings.payment_service_url,
                "kafka_enabled": settings.kafka.enabled,
                "consumer_group": settings.consumer_group,
                "migrations_dir": settings.migrations_dir,
            },
        )

    return create_app(
        settings=settings,
        name="order-service",
        version=__version__,
        title="Order Service API",
        description=(
            "Order intake and lifecycle.\n\n"
            "* `POST /orders` prices the basket against product-service (client prices are ignored), "
            "reserves stock, then writes the order and `order.created` in one transaction.\n"
            "* `Idempotency-Key` is required: a retried POST replays the original response.\n"
            "* `POST /orders/{id}/payments` charges synchronously; the Kafka consumer applies "
            "asynchronous payment results. Both paths converge on the same idempotent saga step.\n"
            "* `POST /orders/{id}/cancel` releases stock and emits `order.cancelled`."
        ),
        routers=(internal_router, router),
        bootstrap=bootstrap,
    )


app = build_app()
