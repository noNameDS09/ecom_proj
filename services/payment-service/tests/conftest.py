"""Fixtures for the payment service tests.

Payment-service has no synchronous dependencies: each test gets a fresh
database and an in-process ASGI app with the deterministic simulated provider
(zero latency, decline rule: cents == 13, blocklist entry).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from payment_service.main import build_app
from payment_service.settings import PaymentServiceSettings

from tests.support.harness import (
    DEFAULT_ADMIN_DSN,
    TEST_INTERNAL_TOKEN,
    TEST_JWT_SECRET,
    admin_dsn_reachable,
    admin_token,
    apply_migrations,
    create_test_database,
    drop_test_database,
)


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    if not admin_dsn_reachable():
        pytest.skip(f"Postgres is not reachable at {DEFAULT_ADMIN_DSN}", allow_module_level=True)
    return DEFAULT_ADMIN_DSN


@pytest.fixture
def payment_database(admin_dsn: str) -> Iterator[tuple[str, str]]:
    name, url = create_test_database("payment_test", admin_dsn=admin_dsn)
    apply_migrations(url, str(_migrations_dir()))
    try:
        yield name, url
    finally:
        drop_test_database(name, admin_dsn=admin_dsn)


@pytest.fixture
def payment_settings(payment_database: tuple[str, str]) -> PaymentServiceSettings:
    _, url = payment_database
    return PaymentServiceSettings(
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
        kafka={"enabled": False},
        provider_latency_seconds=0.0,
        provider_decline_emails="blocked@example.com",
    )


@pytest_asyncio.fixture
async def payment_app(payment_settings: PaymentServiceSettings) -> FastAPI:
    app = build_app(payment_settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def client(payment_app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=payment_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://payment-service") as http_client:
        yield http_client


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Source-Service": "test-harness"}


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token(TEST_JWT_SECRET)}"}


def _migrations_dir() -> Path:
    root = Path(__file__).resolve().parents[1]
    return root / "migrations"
