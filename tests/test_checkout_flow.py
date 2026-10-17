"""End-to-end checkout against the real stack (HTTP between real processes).

Four services run in their own threads with their own databases:

    user-service(8002) -> issues the JWT used everywhere
    product-service    -> catalogue + stock reservations
    payment-service    -> simulated acquirer
    order-service      -> orchestrates the saga

Kafka is disabled here: the payment result comes back over the synchronous
call. The event-driven path is covered by ``test_kafka_pipeline.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

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
    start_service,
)

PASSWORD = "Sup3r-secret-1"


@dataclass
class Stack:
    users: ServiceClient
    products: ServiceClient
    orders: ServiceClient
    payments: ServiceClient
    user_live: LiveService
    product_live: LiveService
    payment_live: LiveService
    order_live: LiveService


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    if not admin_dsn_reachable():
        pytest.skip(f"Postgres is not reachable at {DEFAULT_ADMIN_DSN}", allow_module_level=True)
    return DEFAULT_ADMIN_DSN


@pytest.fixture(scope="session")
def stack(admin_dsn: str) -> Iterator[Stack]:
    databases: list[str] = []
    live: list[LiveService] = []

    def database(prefix: str) -> str:
        name, url = create_test_database(prefix, admin_dsn=admin_dsn)
        databases.append(name)
        return url

    try:
        user_settings = UserServiceSettings(
            database_url=database("e2e_user"),
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
        user_live = start_service(
            app=build_user_app(user_settings), name="user-service", database="user", settings=user_settings
        )
        live.append(user_live)

        product_settings = ProductServiceSettings(
            database_url=database("e2e_product"),
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
            app=build_product_app(product_settings), name="product-service", database="product", settings=product_settings
        )
        live.append(product_live)

        payment_settings = PaymentServiceSettings(
            database_url=database("e2e_payment"),
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
            kafka={"enabled": False},
            provider_latency_seconds=0.0,
        )
        payment_live = start_service(
            app=build_payment_app(payment_settings), name="payment-service", database="payment", settings=payment_settings
        )
        live.append(payment_live)

        order_settings = OrderServiceSettings(
            database_url=database("e2e_order"),
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
            kafka={"enabled": False},
            product_service_url=product_live.url,
            payment_service_url=payment_live.url,
        )
        order_live = start_service(
            app=build_order_app(order_settings), name="order-service", database="order", settings=order_settings
        )
        live.append(order_live)

        users = user_live.client().with_internal_token()
        products = product_live.client().with_internal_token().as_user(admin_token(TEST_JWT_SECRET))
        orders = order_live.client()
        payments = payment_live.client().with_internal_token()
        yield Stack(users=users, products=products, orders=orders, payments=payments, user_live=user_live, product_live=product_live, payment_live=payment_live, order_live=order_live)

        for client in (users, products, orders, payments):
            client.close()
    finally:
        for service in reversed(live):
            service.stop()
        for name in databases:
            drop_test_database(name, admin_dsn=admin_dsn)


# --------------------------------------------------------------------------- helpers


def register_buyer(stack: Stack) -> str:
    """Register a customer through user-service and return their access token."""
    email = f"buyer-{uuid.uuid4().hex[:10]}@example.com"
    response = stack.users.post("/auth/register", json={"email": email, "password": PASSWORD, "full_name": "Test Buyer"})
    assert response.status_code == 201, response.text
    return response.json()["tokens"]["access_token"]


def buyer_headers(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def create_product(stack: Stack, *, price: str, stock: int) -> dict[str, object]:
    sku = f"E2E-{uuid.uuid4().hex[:8].upper()}"
    response = stack.products.post(
        "/products",
        json={
            "sku": sku,
            "name": f"E2E widget {sku}",
            "description": "integration test product",
            "category": "e2e",
            "price": price,
            "currency": "USD",
            "stock": stock,
        },
        headers=buyer_headers(admin_token(TEST_JWT_SECRET)),
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_order(stack: Stack, token: str, product_id: str, quantity: int, *, key: str | None = None) -> dict[str, object]:
    response = stack.orders.post(
        "/orders",
        json={"items": [{"product_id": product_id, "quantity": quantity}]},
        headers=buyer_headers(token, idempotency_key=key or f"e2e-{uuid.uuid4().hex}"),
    )
    assert response.status_code == 201, response.text
    return response.json()


def product_levels(stack: Stack, product_id: str) -> dict[str, object]:
    response = stack.products.get(f"/products/{product_id}/stock")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- tests


def test_checkout_happy_path_across_all_services(stack: Stack) -> None:
    token = register_buyer(stack)
    product = create_product(stack, price="250.00", stock=5)

    key = f"checkout-{uuid.uuid4().hex}"
    order = create_order(stack, token, product["id"], 1, key=key)
    assert order["status"] == "pending"
    assert order["subtotal"] == "250.00"
    assert order["shipping_fee"] == "0.00"
    assert order["total"] == "250.00"
    assert product_levels(stack, product["id"])["available"] == 4

    # A retried POST replays the original order (order-service idempotency).
    replay = stack.orders.post(
        "/orders",
        json={"items": [{"product_id": product["id"], "quantity": 1}]},
        headers=buyer_headers(token, idempotency_key=key),
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == order["id"]
    assert product_levels(stack, product["id"])["available"] == 4

    # Pay synchronously through payment-service.
    payment = stack.orders.post(f"/orders/{order['id']}/payments", headers=buyer_headers(token))
    assert payment.status_code == 200, payment.text
    body = payment.json()
    assert body["applied"] is True
    assert body["payment"]["status"] == "succeeded"
    assert body["payment"]["provider"] == "simulated"
    assert body["order"]["status"] == "paid"

    # The reservation became a stock movement (available unchanged, stock down).
    levels = product_levels(stack, product["id"])
    assert (levels["stock"], levels["reserved"], levels["available"]) == (4, 0, 4)

    # payment-service recorded the payment and can be queried by order id.
    ledger = stack.payments.get(f"/payments?order_id={order['id']}")
    assert ledger.status_code == 200
    assert [entry["status"] for entry in ledger.json()["items"]] == ["succeeded"]

    timeline = stack.orders.get(f"/orders/{order['id']}/timeline", headers=buyer_headers(token)).json()
    assert [entry["to_status"] for entry in timeline["history"]] == ["pending", "paid"]


def test_declined_payment_releases_stock_and_allows_cancellation(stack: Stack) -> None:
    token = register_buyer(stack)
    # Total 100.13: free shipping, cents == 13 -> the simulated acquirer declines.
    product = create_product(stack, price="100.13", stock=3)
    order = create_order(stack, token, product["id"], 1)
    assert product_levels(stack, product["id"])["available"] == 2

    payment = stack.orders.post(f"/orders/{order['id']}/payments", headers=buyer_headers(token))
    assert payment.status_code == 200, payment.text
    body = payment.json()
    assert body["payment"]["status"] == "failed"
    assert body["payment"]["failure_reason"] == "card_declined"
    assert body["order"]["status"] == "payment_failed"

    levels = product_levels(stack, product["id"])
    assert (levels["stock"], levels["reserved"], levels["available"]) == (3, 0, 3)

    cancelled = stack.orders.post(f"/orders/{order['id']}/cancel", json={}, headers=buyer_headers(token))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["order"]["status"] == "cancelled"


def test_cancellation_releases_the_stock_hold(stack: Stack) -> None:
    token = register_buyer(stack)
    product = create_product(stack, price="30.00", stock=4)
    order = create_order(stack, token, product["id"], 3)
    assert product_levels(stack, product["id"])["available"] == 1

    cancelled = stack.orders.post(f"/orders/{order['id']}/cancel", json={"reason": "e2e"}, headers=buyer_headers(token))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["stock_released"] is True
    assert product_levels(stack, product["id"])["available"] == 4


def test_orders_require_a_user_service_token(stack: Stack) -> None:
    product = create_product(stack, price="10.00", stock=1)

    anonymous = stack.orders.post(
        "/orders", json={"items": [{"product_id": product["id"], "quantity": 1}]}, headers={"Idempotency-Key": "anon-e2e"}
    )
    assert anonymous.status_code == 401

    forged = stack.orders.post(
        "/orders",
        json={"items": [{"product_id": product["id"], "quantity": 1}]},
        headers={"Authorization": "Bearer not.a.token", "Idempotency-Key": "forged-e2e"},
    )
    assert forged.status_code == 401
    assert forged.json()["error"]["code"] == "invalid_token"

    # The token user-service issues is accepted by order-service and product-service.
    token = register_buyer(stack)
    order = create_order(stack, token, product["id"], 1)
    fetched = stack.orders.get(f"/orders/{order['id']}", headers=buyer_headers(token))
    assert fetched.status_code == 200
