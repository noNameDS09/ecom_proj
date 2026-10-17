"""Failure handling: what order-service does when product-service is down.

The order service must degrade, not hang: bounded timeouts, retries and a
circuit breaker turn an unreachable dependency into a fast 503, and readiness
reports the dependency as unhealthy.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from order_service.main import build_app
from order_service.settings import OrderServiceSettings

from tests.support.harness import issue_token

# Nothing listens on the discard port.
UNREACHABLE_URL = "http://127.0.0.1:9"


@pytest.fixture
def resilience_settings(order_settings: OrderServiceSettings) -> OrderServiceSettings:
    """Order service pointed at a product-service that is not listening."""
    return order_settings.model_copy(
        update={
            "product_service_url": UNREACHABLE_URL,
            "upstream_connect_timeout_seconds": 0.5,
            "upstream_read_timeout_seconds": 1.0,
            "upstream_retry_attempts": 2,
            "upstream_retry_base_delay_seconds": 0.01,
        }
    )


@pytest_asyncio.fixture
async def resilience_client(resilience_settings: OrderServiceSettings) -> httpx.AsyncClient:
    app: FastAPI = build_app(resilience_settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://order-service") as http_client:
            yield http_client


async def test_order_creation_fails_fast_when_product_service_is_down(resilience_client: httpx.AsyncClient) -> None:
    token = issue_token(email="alice@example.com")
    response = await resilience_client.post(
        "/orders",
        json={"items": [{"product_id": "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "quantity": 1}]},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "resilience-1"},
    )

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "dependency_unavailable"
    assert error["details"]["dependency"] == "product-service"


async def test_readiness_reports_the_product_service_as_unhealthy(resilience_client: httpx.AsyncClient) -> None:
    response = await resilience_client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["product-service"]["status"] == "error"
    assert body["checks"]["database"]["status"] == "ok"
