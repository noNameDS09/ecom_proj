"""Payment API: idempotent charging, deterministic declines, internal auth, events."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from payment_service.consumers import ORDER_CREATED, order_event_handler
from sqlalchemy import text

from common.events import EventAlreadyProcessed
from common.outbox import build_event

PAYMENTS = "/payments"


# --------------------------------------------------------------------------- helpers


def charge_payload(order_id: str | None = None, *, amount: str = "59.99", email: str = "alice@example.com", number: str = "ORD-TEST-1") -> dict[str, Any]:
    return {
        "order_id": order_id or str(uuid.uuid4()),
        "order_number": number,
        "amount": amount,
        "currency": "USD",
        "customer_email": email,
        "description": f"Order {number}",
    }


async def charge(client: httpx.AsyncClient, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
    return await client.post(PAYMENTS, json=payload, headers=headers)


async def outbox_events(app: FastAPI) -> list[tuple[str, str]]:
    async with app.state.rt.db.read_session() as session:
        rows = (await session.execute(text("SELECT event_type, status FROM outbox_events ORDER BY id"))).all()
    return [(row[0], row[1]) for row in rows]


async def wait_until(predicate: Any, *, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} was not satisfied within {timeout}s")


# --------------------------------------------------------------------------- charging


async def test_charge_approved(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    payload = charge_payload()
    response = await charge(client, internal_headers, payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["order_id"] == payload["order_id"]
    assert body["amount"] == "59.99"
    assert body["currency"] == "USD"
    assert body["provider"] == "simulated"
    assert body["provider_reference"].startswith("sim_")
    assert body["failure_reason"] is None
    assert body["source"] == "api"
    assert response.headers["X-Idempotent-Replay"] == "false"
    assert response.headers["Location"] == f"/payments/{body['payment_id']}"


async def test_charge_replay_is_the_same_payment(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    payload = charge_payload()
    first = await charge(client, internal_headers, payload)
    second = await charge(client, internal_headers, payload)

    assert second.status_code == 200, second.text
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert second.json()["provider_reference"] == first.json()["provider_reference"]
    assert second.json()["status"] == "succeeded"


async def test_charge_replay_with_different_terms_conflicts(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    payload = charge_payload()
    await charge(client, internal_headers, payload)

    response = await charge(client, internal_headers, {**payload, "amount": "99.00"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "payment_conflict"
    assert error["details"]["charged_amount"] == "59.99"
    assert error["details"]["requested_amount"] == "99.00"


async def test_card_declined_when_cents_match_the_test_rule(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    response = await charge(client, internal_headers, charge_payload(amount="59.13"))

    # The HTTP call succeeded; the *payment* failed.
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "card_declined"
    assert body["provider_reference"] is None


async def test_provider_limit_declines_the_charge(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    response = await charge(client, internal_headers, charge_payload(amount="5001.00"))
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["failure_reason"] == "amount_limit_exceeded"


async def test_amount_over_the_service_limit_is_rejected(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    response = await charge(client, internal_headers, charge_payload(amount="50001.00"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "amount_limit_exceeded"


async def test_blocklisted_customer_is_declined(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    response = await charge(client, internal_headers, charge_payload(email="blocked@example.com"))
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["failure_reason"] == "card_declined"


# --------------------------------------------------------------------------- reads & auth


async def test_get_and_list_payments(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    payload = charge_payload()
    created = (await charge(client, internal_headers, payload)).json()

    fetched = await client.get(f"{PAYMENTS}/{created['payment_id']}", headers=internal_headers)
    assert fetched.status_code == 200
    assert fetched.json()["order_id"] == payload["order_id"]

    by_order = await client.get(f"{PAYMENTS}?order_id={payload['order_id']}", headers=internal_headers)
    assert by_order.status_code == 200
    assert [item["payment_id"] for item in by_order.json()["items"]] == [created["payment_id"]]
    assert by_order.json()["meta"]["total"] == 1

    by_status = await client.get(f"{PAYMENTS}?status=succeeded", headers=internal_headers)
    assert by_status.json()["meta"]["total"] == 1
    none = await client.get(f"{PAYMENTS}?status=failed", headers=internal_headers)
    assert none.json()["meta"]["total"] == 0

    invalid = await client.get(f"{PAYMENTS}?status=exploded", headers=internal_headers)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_payment_status"

    missing = await client.get(f"{PAYMENTS}/{uuid.uuid4()}", headers=internal_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "payment_not_found"


async def test_endpoints_require_a_service_token(client: httpx.AsyncClient) -> None:
    anonymous = await client.post(PAYMENTS, json=charge_payload())
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "invalid_internal_token"

    wrong_token = await client.get(PAYMENTS, headers={"X-Internal-Token": "nope"})
    assert wrong_token.status_code == 401


# --------------------------------------------------------------------------- events


async def test_outcome_is_published_from_the_outbox(
    client: httpx.AsyncClient, internal_headers: dict[str, str], payment_app: FastAPI
) -> None:
    await charge(client, internal_headers, charge_payload(amount="59.13"))

    async def published() -> bool:
        return ("payment.failed", "published") in await outbox_events(payment_app)

    await wait_until(published, what="payment.failed published from the outbox")


async def test_consumer_charges_orders_and_deduplicates(
    payment_app: FastAPI, client: httpx.AsyncClient, internal_headers: dict[str, str]
) -> None:
    handler = order_event_handler(payment_app.state.payments, consumer="payment-service.order-events")
    order_id = str(uuid.uuid4())
    event = build_event(
        event_type=ORDER_CREATED,
        data={
            "order_id": order_id,
            "order_number": "ORD-CONSUMER-1",
            "user_email": "alice@example.com",
            "total": "120.00",
            "currency": "USD",
        },
        topic="order.events",
        key=order_id,
        producer="order-service",
    )

    await handler(event)

    # A redelivery of the same event is de-duplicated by the inbox.
    try:
        await handler(event)
    except EventAlreadyProcessed:
        pass
    else:
        raise AssertionError("redelivered order.created must raise EventAlreadyProcessed")

    # The consumer and the synchronous API converge on the same payment row.
    replay = await charge(client, internal_headers, charge_payload(order_id=order_id, amount="120.00", number="ORD-CONSUMER-1"))
    assert replay.status_code == 200
    assert replay.json()["source"] == "kafka"
    assert replay.json()["amount"] == "120.00"
