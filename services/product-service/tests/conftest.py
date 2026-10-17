"""Fixtures for the product service tests.

Every test gets its own database (created, migrated, dropped) so tests are
order independent and safe to run in parallel.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from product_service.main import build_app
from product_service.settings import ProductServiceSettings

from tests.support.harness import (
    DEFAULT_ADMIN_DSN,
    TEST_INTERNAL_TOKEN,
    TEST_JWT_SECRET,
    admin_dsn_reachable,
    apply_migrations,
    create_test_database,
    drop_test_database,
)
from tests.support.harness import admin_token as make_admin_token


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    if not admin_dsn_reachable():
        pytest.skip(f"Postgres is not reachable at {DEFAULT_ADMIN_DSN}", allow_module_level=True)
    return DEFAULT_ADMIN_DSN


@pytest.fixture
def product_database(admin_dsn: str) -> Iterator[tuple[str, str]]:
    name, url = create_test_database("product_test", admin_dsn=admin_dsn)
    apply_migrations(url, str(_migrations_dir()))
    try:
        yield name, url
    finally:
        drop_test_database(name, admin_dsn=admin_dsn)


@pytest.fixture
def product_settings(product_database: tuple[str, str]) -> ProductServiceSettings:
    _, url = product_database
    return ProductServiceSettings(
        database_url=url,
        environment="test",
        log_level="WARNING",
        log_format="console",
        jwt_secret=TEST_JWT_SECRET,
        internal_service_token=TEST_INTERNAL_TOKEN,
        metrics_enabled=True,
        access_log_enabled=False,
        run_migrations=True,
        db_connect_attempts=3,
        db_connect_retry_delay_seconds=0.1,
        cache_ttl_seconds=30.0,
    )


@pytest_asyncio.fixture
async def product_app(product_settings: ProductServiceSettings) -> FastAPI:
    app = build_app(product_settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def client(product_app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=product_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://product-service") as http_client:
        yield http_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_admin_token(TEST_JWT_SECRET)}"}


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Source-Service": "test-harness"}


def _migrations_dir() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return root / "migrations"
