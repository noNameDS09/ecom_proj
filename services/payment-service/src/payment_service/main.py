"""Payment service entrypoint.

Run with ``uvicorn payment_service.main:app`` (Docker) or build the ASGI app
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
from payment_service import __version__
from payment_service.api import router
from payment_service.consumers import order_event_handler
from payment_service.provider import SimulatedPaymentProvider
from payment_service.service import PaymentService
from payment_service.settings import PaymentServiceSettings

logger = get_logger("payment_service")


def build_app(settings: PaymentServiceSettings | None = None) -> FastAPI:
    settings = settings or PaymentServiceSettings()
    settings.migrations_dir = resolve_migrations_dir(settings.migrations_dir, package_file=__file__)

    async def bootstrap(app: FastAPI, runtime: Runtime) -> None:
        provider = SimulatedPaymentProvider(settings)
        payments = PaymentService(runtime.db, provider, settings)
        app.state.payments = payments
        app.state.provider = provider

        # -- eventing: outbox relay publishes payment.events
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

        # -- eventing: consume order.events and charge
        if settings.kafka.enabled:
            consumer = EventConsumer(
                settings=settings.kafka,
                service_name=settings.service_name,
                group_id=settings.consumer_group,
                topics=[settings.order_events_topic],
                handler=order_event_handler(payments, consumer=settings.consumer_group),
                on_dlq=make_dlq_handler(
                    database=runtime.db,
                    publisher=publisher,
                    service_name=settings.service_name,
                    dlq_topic=settings.kafka.dlq_topic,
                ),
            )
            await runtime.start_consumer(consumer, name="order-events")
            health.register_readiness(app, "kafka", publisher.healthy)

        logger.info(
            "payment service bootstrapped",
            extra={
                "provider": provider.name,
                "provider_latency_seconds": settings.provider_latency_seconds,
                "decline_cents": settings.provider_decline_cents,
                "kafka_enabled": settings.kafka.enabled,
                "consumer_group": settings.consumer_group,
                "migrations_dir": settings.migrations_dir,
            },
        )

    return create_app(
        settings=settings,
        name="payment-service",
        version=__version__,
        title="Payment Service API",
        description=(
            "Charges orders through a simulated acquirer.\n\n"
            "* `POST /payments` is idempotent by order id: the unique constraint on "
            "`payments.order_id` makes double charging impossible.\n"
            "* Declines are deterministic and configurable (amounts ending in .13, "
            "an amount limit, a customer blocklist).\n"
            "* `payment.succeeded` / `payment.failed` are written to the outbox in the "
            "same transaction as the payment, so order-service always learns the outcome."
        ),
        routers=(router,),
        bootstrap=bootstrap,
    )


app = build_app()
