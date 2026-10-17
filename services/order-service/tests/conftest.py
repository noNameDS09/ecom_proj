"""Fixtures for the order service tests.

Every test gets:

* a live **product-service** (real HTTP, real Postgres) shared for the session;
  products are created per test with unique SKUs so tests stay independent;
* a fresh **order-service database** and an in-process ASGI app bound to the
  live product service.

Kafka is disabled here: the payment saga is driven through the internal
payment-result endpoint (the same code path the Kafka consumer calls), and the
cross-service suite exercises the real broker when one is available.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from order_service.main import build_app as build_order_app
from order_service.settings import OrderServiceSettings
from product_service.main import build_app as build_product_app
from product_service.settings import ProductServiceSettings

from tests.support.harness import (
    DEFAULT_ADMIN_DSN,
    TEST_INTERNAL_TOKEN,
    TEST_JWT_SECRET,
    LiveService,
    ServiceClient,
    admin_dsn_reachable,
    admin_token,
    apply_migrations,
    create_test_database,
    drop_test_database,
    issue_token,
    start_service,
)

# A port nothing listens on: proves the pay endpoint maps an unreachable
# payment-service to a 503 instead of hanging or crashing.
UNREACHABLE_PAYMENT_URL = "http://127.0.0.1:9"


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    if not admin_dsn_reachable():
        pytest.skip(f"Postgres is not reachable at {DEFAULT_ADMIN_DSN}", allow_module_level=True)
    return DEFAULT_ADMIN_DSN


# --------------------------------------------------------------------------- product service


@pytest.fixture(scope="session")
def product_live(admin_dsn: str) -> Iterator[LiveService]:
    name, url = create_test_database("order_product_test", admin_dsn=admin_dsn)
    settings = ProductServiceSettings(
        database_url=url,
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
    live = start_service(
        app=build_product_app(settings),
        name="product-service",
        database=name,
        settings=settings,
        startup_timeout=60.0,
    )
    try:
        yield live
    finally:
        live.stop()
        drop_test_database(name, admin_dsn=admin_dsn)


@pytest.fixture(scope="session")
def product_service(product_live: LiveService) -> Iterator[ServiceClient]:
    """Admin + internal-token client for the live product service."""
    client = product_live.client().with_internal_token()
    client.as_user(admin_token(TEST_JWT_SECRET))
    try:
        yield client
    finally:
        client.close()


# --------------------------------------------------------------------------- order service


@pytest.fixture
def order_database(admin_dsn: str) -> Iterator[tuple[str, str]]:
    name, url = create_test_database("order_test", admin_dsn=admin_dsn)
    apply_migrations(url, str(_migrations_dir()))
    try:
        yield name, url
    finally:
        drop_test_database(name, admin_dsn=admin_dsn)


@pytest.fixture
def order_settings(order_database: tuple[str, str], product_live: LiveService) -> OrderServiceSettings:
    _, url = order_database
    return OrderServiceSettings(
        database_url=url,
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
        payment_service_url=UNREACHABLE_PAYMENT_URL,
        kafka={"enabled": False},
        upstream_connect_timeout_seconds=1.0,
        upstream_read_timeout_seconds=3.0,
        upstream_retry_attempts=2,
        upstream_retry_base_delay_seconds=0.05,
    )


@pytest_asyncio.fixture
async def order_app(order_settings: OrderServiceSettings) -> FastAPI:
    app = build_order_app(order_settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def client(order_app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=order_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://order-service") as http_client:
        yield http_client


# --------------------------------------------------------------------------- identities


@pytest.fixture
def alice_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def alice_headers(alice_id: str) -> dict[str, str]:
    token = issue_token(user_id=alice_id, email="alice@example.com", roles=("customer",))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bob_headers() -> dict[str, str]:
    token = issue_token(user_id=str(uuid.uuid4()), email="bob@example.com", roles=("customer",))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token(TEST_JWT_SECRET)}"}


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Source-Service": "test-harness"}


def _migrations_dir() -> Path:
    root = Path(__file__).resolve().parents[1]
    return root / "migrations"
