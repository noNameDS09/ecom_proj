"""The payment saga: idempotent transitions, compensation and events.

Payment results are applied through the internal ``payment-result`` endpoint —
the same ``OrderService.apply_payment_result`` the Kafka consumer calls — so the
saga is fully exercised without a broker.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from tests.support.harness import ServiceClient

ORDERS = "/orders"


# --------------------------------------------------------------------------- helpers


def create_product(product_service: ServiceClient, *, sku: str, stock: int, price: str = "10.00") -> dict[str, Any]:
    response = product_service.post(
        "/products",
        json={
            "sku": sku,
            "name": f"Widget {sku}",
            "description": "saga test product",
            "category": "test-widgets",
            "price": price,
            "currency": "USD",
            "stock": stock,
            "active": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def place_order(client: httpx.AsyncClient, headers: dict[str, str], product_id: str, quantity: int = 1) -> dict[str, Any]:
    response = await client.post(
        ORDERS,
        json={"items": [{"product_id": product_id, "quantity": quantity}]},
        headers={**headers, "Idempotency-Key": f"saga-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def apply_payment(
    client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    order_id: str,
    *,
    outcome: str,
    payment_id: str | None = None,
    event_id: str | None = None,
    reason: str | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"outcome": outcome}
    if payment_id:
        payload["payment_id"] = payment_id
    if event_id:
        payload["event_id"] = event_id
    if reason:
        payload["reason"] = reason
    return await client.post(f"/internal/orders/{order_id}/payment-result", json=payload, headers=internal_headers)


async def stock_of(product_service: ServiceClient, product_id: str) -> dict[str, Any]:
    response = product_service.get(f"/products/{product_id}/stock")
    assert response.status_code == 200, response.text
    return response.json()


async def outbox_events(app: FastAPI) -> list[tuple[str, str]]:
    async with app.state.rt.db.read_session() as session:
        rows = (await session.execute(text("SELECT event_type, status FROM outbox_events ORDER BY id"))).all()
    return [(row[0], row[1]) for row in rows]


async def wait_until(predicate: Callable[[], Awaitable[bool]], *, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} was not satisfied within {timeout}s")


# --------------------------------------------------------------------------- success


async def test_successful_payment_commits_the_reservation(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-PAID", stock=6, price="20.00")
    order = await place_order(client, alice_headers, product["id"], quantity=2)
    assert (await stock_of(product_service, product["id"]))["available"] == 4

    response = await apply_payment(client, internal_headers, order["id"], outcome="succeeded", payment_id="pay_test_ok")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["order"]["status"] == "paid"
    assert body["order"]["paid_at"] is not None

    # The hold became a real stock movement: `stock` drops, `available` stays.
    levels = await stock_of(product_service, product["id"])
    assert levels["stock"] == 4
    assert levels["reserved"] == 0
    assert levels["available"] == 4

    timeline = (await client.get(f"{ORDERS}/{order['id']}/timeline", headers=alice_headers)).json()
    assert [(entry["to_status"], entry["source"]) for entry in timeline["history"]] == [("pending", "api"), ("paid", "api")]
    assert timeline["history"][0]["from_status"] is None  # created: nothing -> pending
    assert timeline["history"][1]["from_status"] == "pending"
    assert timeline["history"][-1]["actor"] == "payment:pay_test_ok"
    assert "payment pay_test_ok" in timeline["history"][-1]["reason"]


async def test_successful_payment_emits_order_paid_event(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient, order_app: FastAPI
) -> None:
    product = create_product(product_service, sku="SAGA-EVENT", stock=3)
    order = await place_order(client, alice_headers, product["id"])
    applied = await apply_payment(client, internal_headers, order["id"], outcome="succeeded", payment_id="pay_evt")
    assert applied.status_code == 200 and applied.json()["applied"] is True

    async def paid_event_published() -> bool:
        events = await outbox_events(order_app)
        return ("order.created", "published") in events and ("order.paid", "published") in events

    await wait_until(paid_event_published, what="order.created + order.paid published from the outbox")


# --------------------------------------------------------------------------- failure & compensation


async def test_failed_payment_releases_the_reservation(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-DECLINED", stock=6, price="13.13")
    order = await place_order(client, alice_headers, product["id"], quantity=2)

    response = await apply_payment(
        client, internal_headers, order["id"], outcome="failed", payment_id="pay_test_declined", reason="card_declined"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["order"]["status"] == "payment_failed"

    levels = await stock_of(product_service, product["id"])
    assert levels["stock"] == 6
    assert levels["reserved"] == 0
    assert levels["available"] == 6

    timeline = (await client.get(f"{ORDERS}/{order['id']}/timeline", headers=alice_headers)).json()
    assert timeline["history"][-1]["to_status"] == "payment_failed"
    assert timeline["history"][-1]["reason"] == "card_declined"


async def test_a_failed_order_can_still_be_cancelled(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-RETRY", stock=2)
    order = await place_order(client, alice_headers, product["id"])
    await apply_payment(client, internal_headers, order["id"], outcome="failed", reason="insufficient_funds")

    cancel = await client.post(f"{ORDERS}/{order['id']}/cancel", json={"reason": "giving up"}, headers=alice_headers)
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["order"]["status"] == "cancelled"
    # The failed payment already released the hold, so the release is a no-op.
    assert cancel.json()["release_outcome"] == "already_released"


# --------------------------------------------------------------------------- idempotency & ordering


async def test_redelivery_of_the_same_event_is_a_noop(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-DEDUP", stock=5)
    order = await place_order(client, alice_headers, product["id"])
    event_id = f"evt-{uuid.uuid4().hex}"

    first = await apply_payment(client, internal_headers, order["id"], outcome="succeeded", payment_id="pay_d1", event_id=event_id)
    assert first.json()["applied"] is True

    replay = await apply_payment(client, internal_headers, order["id"], outcome="succeeded", payment_id="pay_d1", event_id=event_id)
    assert replay.status_code == 200, replay.text
    assert replay.json()["applied"] is False
    assert replay.json()["order"]["status"] == "paid"

    levels = await stock_of(product_service, product["id"])
    assert (levels["stock"], levels["reserved"]) == (4, 0)


async def test_a_new_delivery_for_an_already_final_order_is_ignored(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-FINAL", stock=5)
    order = await place_order(client, alice_headers, product["id"])
    await apply_payment(client, internal_headers, order["id"], outcome="succeeded", payment_id="pay_f1")

    # A late `failed` from a different (duplicated) delivery must not flip a paid order.
    late = await apply_payment(client, internal_headers, order["id"], outcome="failed", payment_id="pay_f2", reason="late_duplicate")
    assert late.status_code == 200
    assert late.json()["applied"] is False
    assert late.json()["order"]["status"] == "paid"

    levels = await stock_of(product_service, product["id"])
    assert (levels["stock"], levels["reserved"]) == (4, 0)


async def test_payment_after_cancellation_does_not_resurrect_the_order(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-LATE", stock=5)
    order = await place_order(client, alice_headers, product["id"])
    await client.post(f"{ORDERS}/{order['id']}/cancel", json={}, headers=alice_headers)

    late = await apply_payment(client, internal_headers, order["id"], outcome="succeeded", payment_id="pay_late")
    assert late.status_code == 200, late.text
    assert late.json()["applied"] is False
    assert late.json()["order"]["status"] == "cancelled"

    levels = await stock_of(product_service, product["id"])
    assert (levels["stock"], levels["reserved"], levels["available"]) == (5, 0, 5)


async def test_a_paid_order_cannot_be_cancelled(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-NOCANCEL", stock=3)
    order = await place_order(client, alice_headers, product["id"])
    await apply_payment(client, internal_headers, order["id"], outcome="succeeded")

    response = await client.post(f"{ORDERS}/{order['id']}/cancel", json={}, headers=alice_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_not_cancellable"


# --------------------------------------------------------------------------- internal endpoint auth


async def test_payment_result_endpoint_requires_the_internal_token(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-TOKEN", stock=2)
    order = await place_order(client, alice_headers, product["id"])

    anonymous = await client.post(
        f"/internal/orders/{order['id']}/payment-result", json={"outcome": "succeeded"}, headers={"X-Internal-Token": "nope"}
    )
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "invalid_internal_token"


# --------------------------------------------------------------------------- synchronous pay endpoint


async def test_pay_endpoint_rejects_orders_that_are_not_pending(
    client: httpx.AsyncClient, alice_headers: dict[str, str], internal_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-PAY-STATE", stock=2)
    order = await place_order(client, alice_headers, product["id"])
    await apply_payment(client, internal_headers, order["id"], outcome="succeeded")

    response = await client.post(f"{ORDERS}/{order['id']}/payments", headers=alice_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_not_payable"


async def test_pay_endpoint_maps_an_unreachable_payment_service_to_503(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-PAY-DOWN", stock=2)
    order = await place_order(client, alice_headers, product["id"])

    # The fixture points payment-service at a closed port.
    response = await client.post(f"{ORDERS}/{order['id']}/payments", headers=alice_headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"
    # The order is untouched and can still be paid once the dependency recovers.
    order_after = (await client.get(f"{ORDERS}/{order['id']}", headers=alice_headers)).json()
    assert order_after["status"] == "pending"


# --------------------------------------------------------------------------- fulfilment


async def test_admin_fulfils_a_paid_order_idempotently(
    client: httpx.AsyncClient,
    alice_headers: dict[str, str],
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
    product_service: ServiceClient,
    order_app: FastAPI,
) -> None:
    product = create_product(product_service, sku="SAGA-FULFIL", stock=3)
    order = await place_order(client, alice_headers, product["id"])
    applied = await apply_payment(client, internal_headers, order["id"], outcome="succeeded")
    assert applied.status_code == 200, applied.text

    first = await client.post(f"{ORDERS}/{order['id']}/fulfil", headers=admin_headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "fulfilled"

    # Idempotent: a second fulfil is a no-op, not a conflict or a second event.
    second = await client.post(f"{ORDERS}/{order['id']}/fulfil", headers=admin_headers)
    assert second.status_code == 200
    assert second.json()["status"] == "fulfilled"

    timeline = await client.get(f"{ORDERS}/{order['id']}/timeline", headers=admin_headers)
    history = timeline.json()["history"]
    assert [entry["to_status"] for entry in history] == ["pending", "paid", "fulfilled"]
    assert history[-1]["from_status"] == "paid"
    assert history[-1]["source"] == "api"

    events = await outbox_events(order_app)
    fulfilled_events = [status for name, status in events if name == "order.fulfilled"]
    assert len(fulfilled_events) == 1


async def test_fulfil_requires_admin_and_a_paid_order(
    client: httpx.AsyncClient, alice_headers: dict[str, str], admin_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="SAGA-FULFIL-GUARD", stock=3)
    order = await place_order(client, alice_headers, product["id"])

    unauthenticated = await client.post(f"{ORDERS}/{order['id']}/fulfil")
    assert unauthenticated.status_code == 401

    forbidden = await client.post(f"{ORDERS}/{order['id']}/fulfil", headers=alice_headers)
    assert forbidden.status_code == 403

    pending = await client.post(f"{ORDERS}/{order['id']}/fulfil", headers=admin_headers)
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "order_not_fulfillable"
    assert pending.json()["error"]["details"]["status"] == "pending"

    unknown = await client.post(f"{ORDERS}/{uuid.uuid4()}/fulfil", headers=admin_headers)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "order_not_found"
