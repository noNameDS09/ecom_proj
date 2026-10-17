"""Focused tests for the resilient HTTP client, using a mock transport.

These cover the retry/error-mapping paths that only run against a live upstream
in the service test suites: retrying retryable statuses, not retrying unsafe
methods or business 4xx answers, and surfacing upstream error envelopes.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from common.errors import AppError, UpstreamError
from common.httpclient import HttpClient, RetryPolicy

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> HttpClient:
    return HttpClient(
        name="upstream",
        base_url="http://upstream.test",
        service_name="test-service",
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(attempts=3, base_delay=0.0, max_delay=0.0, jitter=0.0),
    )


async def test_retries_a_retryable_status_then_succeeds() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"code": "overloaded", "message": "try later"}})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    try:
        response = await client.request("GET", "/things")
    finally:
        await client.close()

    assert response.status_code == 200
    assert len(calls) == 2
    assert client.retries_total == 1


async def test_retryable_status_exhausts_attempts_and_raises() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502, json={"error": {"code": "bad_gateway", "message": "nope"}})

    client = _client(handler)
    try:
        with pytest.raises(UpstreamError) as exc:
            await client.request("GET", "/things")
    finally:
        await client.close()

    assert len(calls) == 3
    assert exc.value.code == "bad_gateway"
    assert exc.value.details["upstream_status"] == 502
    assert client.retries_total == 2  # two retries before giving up


async def test_unsafe_methods_are_not_retried_by_default() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": {"code": "boom", "message": "boom"}})

    client = _client(handler)
    try:
        with pytest.raises(UpstreamError):
            await client.request("POST", "/things", json={"x": 1})
    finally:
        await client.close()

    assert len(calls) == 1


async def test_business_4xx_is_not_retried_and_preserves_the_envelope() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(409, json={"error": {"code": "out_of_stock", "message": "not enough", "details": {"sku": "S1"}}})

    client = _client(handler)
    try:
        with pytest.raises(AppError) as exc:
            await client.request("GET", "/things")
    finally:
        await client.close()

    assert len(calls) == 1
    assert exc.value.status_code == 409
    assert exc.value.code == "out_of_stock"
    assert exc.value.details["sku"] == "S1"
