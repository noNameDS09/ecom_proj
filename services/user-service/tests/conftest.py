"""Fixtures for the user service tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from user_service.main import build_app
from user_service.settings import UserServiceSettings

from tests.support.harness import (
    DEFAULT_ADMIN_DSN,
    TEST_INTERNAL_TOKEN,
    TEST_JWT_SECRET,
    admin_dsn_reachable,
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
def user_database(admin_dsn: str) -> Iterator[tuple[str, str]]:
    name, url = create_test_database("user_test", admin_dsn=admin_dsn)
    apply_migrations(url, str(Path(__file__).resolve().parents[1] / "migrations"))
    try:
        yield name, url
    finally:
        drop_test_database(name, admin_dsn=admin_dsn)


@pytest.fixture
def user_settings(user_database: tuple[str, str]) -> UserServiceSettings:
    _, url = user_database
    return UserServiceSettings(
        database_url=url,
        environment="test",
        log_level="WARNING",
        log_format="console",
        jwt_secret=TEST_JWT_SECRET,
        internal_service_token=TEST_INTERNAL_TOKEN,
        # bcrypt rounds are deliberately low here: hashing is the slow part of auth.
        bcrypt_rounds=4,
        access_token_ttl_seconds=900,
        metrics_enabled=True,
        access_log_enabled=False,
        run_migrations=True,
        db_connect_attempts=3,
        db_connect_retry_delay_seconds=0.1,
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )


@pytest_asyncio.fixture
async def user_app(user_settings: UserServiceSettings) -> FastAPI:
    app = build_app(user_settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def client(user_app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://user-service") as http_client:
        yield http_client


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Source-Service": "test-harness"}
