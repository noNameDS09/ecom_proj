"""Event-driven pipeline test: order.created -> payment -> payment.events -> saga.

Requires a Kafka broker. Run with::

    TEST_KAFKA_BOOTSTRAP=localhost:19092 pytest tests/test_kafka_pipeline.py -m kafka

Nothing here calls ``POST /orders/{id}/payments``: the order must reach ``paid``
(or ``payment_failed``) purely through the event flow —

    order-service outbox -> order.events -> payment-service (charges)
    payment-service outbox -> payment.events -> order-service (applies the result)

Each run uses its own topic names and consumer groups, so a broker shared with
other runs never replays someone else's history.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from order_service.main import build_app as build_order_app
from order_service.settings import OrderServiceSettings
from payment_service.main import build_app as build_payment_app
from payment_service.settings import PaymentServiceSettings
from product_service.main import build_app as build_product_app
from product_service.settings import ProductServiceSettings
from user_service.main import build_app as build_user_app
from user_service.settings import UserServiceSettings

from tests.support.harness import (
    DEFAULT_ADMIN_DSN,
    TEST_INTERNAL_TOKEN,
    TEST_JWT_SECRET,
    LiveService,
    ServiceClient,
    admin_dsn_reachable,
    admin_token,
    create_test_database,
    drop_test_database,
    kafka_bootstrap,
    start_service,
    wait_until,
)

pytestmark = pytest.mark.kafka

PASSWORD = "Sup3r-secret-1"


@dataclass
class Pipeline:
    users: ServiceClient
    products: ServiceClient
    orders: ServiceClient
    payments: ServiceClient
    order_database: str
    payment_database: str
    live: list[LiveService]


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    if not admin_dsn_reachable():
        pytest.skip(f"Postgres is not reachable at {DEFAULT_ADMIN_DSN}", allow_module_level=True)
    return DEFAULT_ADMIN_DSN


@pytest.fixture(scope="session")
def pipeline(admin_dsn: str) -> Iterator[Pipeline]:
    bootstrap = kafka_bootstrap()
    if not bootstrap:
        pytest.skip("Set TEST_KAFKA_BOOTSTRAP to run the Kafka pipeline tests")

    run = uuid.uuid4().hex[:8]
    order_topic = f"e2e.{run}.order.events"
    payment_topic = f"e2e.{run}.payment.events"
    dlq_topic = f"e2e.{run}.dlq"

    databases: list[str] = []
    live: list[LiveService] = []

    def database(prefix: str) -> tuple[str, str]:
        name, url = create_test_database(prefix, admin_dsn=admin_dsn)
        databases.append(name)
        return name, url

    def kafka_settings(**overrides: Any) -> dict[str, Any]:
        return {
            "enabled": True,
            "bootstrap_servers": bootstrap,
            "auto_offset_reset": "earliest",
            "max_handler_attempts": 6,
            "handler_backoff_seconds": 0.3,
            "dlq_topic": dlq_topic,
            **overrides,
        }

    try:
        user_name, user_url = database("kafka_user")
        user_settings = UserServiceSettings(
            database_url=user_url,
            environment="test",
            log_level="WARNING",
            log_format="console",
            jwt_secret=TEST_JWT_SECRET,
            internal_service_token=TEST_INTERNAL_TOKEN,
            metrics_enabled=False,
            access_log_enabled=False,
            run_migrations=True,
            db_connect_attempts=3,
            db_connect_retry_delay_seconds=0.1,
            bootstrap_admin_email="",
        )
        user_live = start_service(app=build_user_app(user_settings), name="user-service", database=user_name, settings=user_settings)
        live.append(user_live)

        product_name, product_url = database("kafka_product")
        product_settings = ProductServiceSettings(
            database_url=product_url,
            environment="test",
            log_level="WARNING",
            log_format="console",
            jwt_secret=TEST_JWT_SECRET,
            internal_service_token=TEST_INTERNAL_TOKEN,
            metrics_enabled=False,
            access_log_enabled=False,
            run_migrations=True,
            db_connect_attempts=3,
            db_connect_retry_delay_seconds=0.1,
        )
        product_live = start_service(
            app=build_product_app(product_settings), name="product-service", database=product_name, settings=product_settings
        )
        live.append(product_live)

        payment_name, payment_url = database("kafka_payment")
        payment_settings = PaymentServiceSettings(
            database_url=payment_url,
            environment="test",
            log_level="WARNING",
            log_format="console",
            jwt_secret=TEST_JWT_SECRET,
            internal_service_token=TEST_INTERNAL_TOKEN,
            metrics_enabled=False,
            access_log_enabled=False,
            run_migrations=True,
            db_connect_attempts=3,
            db_connect_retry_delay_seconds=0.1,
            provider_latency_seconds=0.0,
            order_events_topic=order_topic,
            payment_events_topic=payment_topic,
            consumer_group=f"e2e.{run}.payment-order-events",
            kafka=kafka_settings(),
        )
        payment_live = start_service(
            app=build_payment_app(payment_settings), name="payment-service", database=payment_name, settings=payment_settings
        )
        live.append(payment_live)

        order_name, order_url = database("kafka_order")
        order_settings = OrderServiceSettings(
            database_url=order_url,
            environment="test",
            log_level="WARNING",
            log_format="console",
            jwt_secret=TEST_JWT_SECRET,
            internal_service_token=TEST_INTERNAL_TOKEN,
            metrics_enabled=False,
            access_log_enabled=False,
            run_migrations=True,
            db_connect_attempts=3,
            db_connect_retry_delay_seconds=0.1,
            product_service_url=product_live.url,
            payment_service_url=payment_live.url,
            order_events_topic=order_topic,
            payment_events_topic=payment_topic,
            consumer_group=f"e2e.{run}.order-payments",
            kafka=kafka_settings(),
        )
        order_live = start_service(app=build_order_app(order_settings), name="order-service", database=order_name, settings=order_settings)
        live.append(order_live)

        users = user_live.client().with_internal_token()
        products = product_live.client().with_internal_token().as_user(admin_token(TEST_JWT_SECRET))
        orders = order_live.client()
        payments = payment_live.client().with_internal_token()
        yield Pipeline(
            users=users,
            products=products,
            orders=orders,
            payments=payments,
            order_database=order_name,
            payment_database=payment_name,
            live=live,
        )
        for client in (users, products, orders, payments):
            client.close()
    finally:
        for service in reversed(live):
            service.stop()
        for name in databases:
            drop_test_database(name, admin_dsn=admin_dsn)


# --------------------------------------------------------------------------- helpers


def register_buyer(pipeline: Pipeline) -> str:
    email = f"kafka-buyer-{uuid.uuid4().hex[:10]}@example.com"
    response = pipeline.users.post("/auth/register", json={"email": email, "password": PASSWORD, "full_name": "Kafka Buyer"})
    assert response.status_code == 201, response.text
    return response.json()["tokens"]["access_token"]


def create_product(pipeline: Pipeline, *, price: str, stock: int) -> dict[str, Any]:
    sku = f"KAFKA-{uuid.uuid4().hex[:8].upper()}"
    response = pipeline.products.post(
        "/products",
        json={
            "sku": sku,
            "name": f"Kafka widget {sku}",
            "description": "kafka pipeline product",
            "category": "e2e",
            "price": price,
            "currency": "USD",
            "stock": stock,
        },
        headers={"Authorization": f"Bearer {admin_token(TEST_JWT_SECRET)}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sql_query(database: str, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    dsn = DEFAULT_ADMIN_DSN.rsplit("/", 1)[0] + f"/{database}"
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def wait_for_order_status(pipeline: Pipeline, token: str, order_id: str, expected: str, *, timeout: float = 90.0) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def read_status() -> bool:
        response = pipeline.orders.get(f"/orders/{order_id}", headers=auth(token))
        if response.status_code != 200:
            return False
        result.clear()
        result.update(response.json())
        return result["status"] == expected

    wait_until(read_status, timeout=timeout, interval=0.5, what=f"order to reach status '{expected}' via Kafka")
    return result


# --------------------------------------------------------------------------- tests


def test_order_created_is_paid_through_the_event_pipeline(pipeline: Pipeline) -> None:
    token = register_buyer(pipeline)
    product = create_product(pipeline, price="42.00", stock=2)

    created = pipeline.orders.post(
        "/orders",
        json={"items": [{"product_id": product["id"], "quantity": 1}]},
        headers={**auth(token), "Idempotency-Key": f"kafka-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["status"] == "pending"

    paid = wait_for_order_status(pipeline, token, order["id"], "paid")
    assert paid["paid_at"] is not None

    # The payment was processed by payment-service and applied by the consumer.
    ledger = pipeline.payments.get(f"/payments?order_id={order['id']}")
    assert [entry["status"] for entry in ledger.json()["items"]] == ["succeeded"]

    timeline = pipeline.orders.get(f"/orders/{order['id']}/timeline", headers=auth(token)).json()
    assert [(entry["to_status"], entry["source"]) for entry in timeline["history"]] == [("pending", "api"), ("paid", "kafka")]

    # Both services drained their outbox; the chain is closed.
    order_events = sql_query(pipeline.order_database, "SELECT event_type, status FROM outbox_events ORDER BY id")
    assert ("order.created", "published") in order_events
    assert ("order.paid", "published") in order_events
    payment_events = sql_query(pipeline.payment_database, "SELECT event_type, status FROM outbox_events ORDER BY id")
    assert ("payment.succeeded", "published") in payment_events

    levels = pipeline.products.get(f"/products/{product['id']}/stock").json()
    assert (levels["stock"], levels["reserved"]) == (1, 0)


def test_declined_payment_fails_the_order_through_the_event_pipeline(pipeline: Pipeline) -> None:
    token = register_buyer(pipeline)
    # Cents == 13 declines; free shipping keeps the total at 100.13.
    product = create_product(pipeline, price="100.13", stock=2)

    created = pipeline.orders.post(
        "/orders",
        json={"items": [{"product_id": product["id"], "quantity": 1}]},
        headers={**auth(token), "Idempotency-Key": f"kafka-{uuid.uuid4().hex}"},
    )
    order = created.json()

    failed = wait_for_order_status(pipeline, token, order["id"], "payment_failed")
    assert failed["status"] == "payment_failed"

    # The failed saga released the hold: the stock is sellable again.
    levels = pipeline.products.get(f"/products/{product['id']}/stock").json()
    assert (levels["stock"], levels["reserved"], levels["available"]) == (2, 0, 2)

    ledger = pipeline.payments.get(f"/payments?order_id={order['id']}")
    entry = ledger.json()["items"][0]
    assert entry["status"] == "failed"

    # No dead letters were produced by either consumer.
    assert sql_query(pipeline.order_database, "SELECT count(*) FROM failed_events") == [(0,)]
    assert sql_query(pipeline.payment_database, "SELECT count(*) FROM failed_events") == [(0,)]
