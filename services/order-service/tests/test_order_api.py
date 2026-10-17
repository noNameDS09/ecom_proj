"""Order API: pricing, idempotent creation, authorization, cancellation."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.support.harness import ServiceClient

ORDERS = "/orders"


# --------------------------------------------------------------------------- helpers


def create_product(
    product_service: ServiceClient, *, sku: str, stock: int, price: str = "10.00", active: bool = True
) -> dict[str, Any]:
    response = product_service.post(
        "/products",
        json={
            "sku": sku,
            "name": f"Widget {sku}",
            "description": "order service test product",
            "category": "test-widgets",
            "price": price,
            "currency": "USD",
            "stock": stock,
            "active": active,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def stock_of(product_service: ServiceClient, product_id: str) -> dict[str, Any]:
    response = product_service.get(f"/products/{product_id}/stock")
    assert response.status_code == 200, response.text
    return response.json()


async def place_order(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    items: list[dict[str, Any]],
    idempotency_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"items": items, **(extra or {})}
    merged = {**headers, "Idempotency-Key": idempotency_key or f"test-{uuid.uuid4().hex}"}
    return await client.post(ORDERS, json=payload, headers=merged)


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


# --------------------------------------------------------------------------- pricing


async def test_create_order_prices_from_catalogue(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient, alice_id: str
) -> None:
    product = create_product(product_service, sku="ORD-PRICE", stock=10, price="25.00")
    response = await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 2}])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["user_id"] == alice_id
    assert body["currency"] == "USD"
    assert body["subtotal"] == "50.00"
    assert body["shipping_fee"] == "9.99"
    assert body["total"] == "59.99"
    assert body["item_count"] == 1
    assert body["order_number"].startswith("ORD-")
    assert response.headers["X-Idempotent-Replay"] == "false"
    assert response.headers["Location"] == f"/orders/{body['id']}"

    item = body["items"][0]
    assert item["sku"] == "ORD-PRICE"
    assert item["name"] == product["name"]
    assert item["unit_price"] == "25.00"
    assert item["line_total"] == "50.00"

    levels = stock_of(product_service, product["id"])
    assert levels["stock"] == 10
    assert levels["reserved"] == 2
    assert levels["available"] == 8


async def test_shipping_is_free_above_the_threshold(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-FREE-SHIP", stock=5, price="120.00")
    response = await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 1}])

    body = response.json()
    assert body["subtotal"] == "120.00"
    assert body["shipping_fee"] == "0.00"
    assert body["total"] == "120.00"


async def test_preview_prices_without_reserving(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-PREVIEW", stock=3, price="40.00")

    response = await client.post(
        f"{ORDERS}/preview",
        json={"items": [{"product_id": product["id"], "quantity": 4}]},
        headers=alice_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subtotal"] == "160.00"
    assert body["item_count"] == 1
    line = body["lines"][0]
    assert line["sku"] == "ORD-PREVIEW"
    assert line["available"] == 3
    assert line["requested"] == 4
    assert line["line_total"] == "160.00"
    assert body["all_available"] is False

    # A preview must never move stock.
    assert stock_of(product_service, product["id"])["available"] == 3


# --------------------------------------------------------------------------- idempotency


async def test_create_order_requires_idempotency_key(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-NO-KEY", stock=5)
    response = await client.post(ORDERS, json={"items": [{"product_id": product["id"], "quantity": 1}]}, headers=alice_headers)

    assert response.status_code == 422
    assert error_code(response) == "idempotency_key_required"
    assert response.json()["error"]["details"] == {"header": "Idempotency-Key"}


async def test_retried_create_replays_the_original_order(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-REPLAY", stock=10, price="15.00")
    key = f"replay-{uuid.uuid4().hex}"
    items = [{"product_id": product["id"], "quantity": 2}]

    first = await place_order(client, alice_headers, items=items, idempotency_key=key)
    assert first.status_code == 201, first.text
    second = await place_order(client, alice_headers, items=items, idempotency_key=key)

    assert second.status_code == 200, second.text
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["order_number"] == first.json()["order_number"]
    # Stock was only reserved once.
    assert stock_of(product_service, product["id"])["reserved"] == 2


async def test_reusing_a_key_with_a_different_payload_conflicts(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    first_product = create_product(product_service, sku="ORD-REUSE-A", stock=5)
    second_product = create_product(product_service, sku="ORD-REUSE-B", stock=5)
    key = f"reuse-{uuid.uuid4().hex}"

    first = await place_order(client, alice_headers, items=[{"product_id": first_product["id"], "quantity": 1}], idempotency_key=key)
    assert first.status_code == 201, first.text

    conflicting = await place_order(
        client, alice_headers, items=[{"product_id": second_product["id"], "quantity": 3}], idempotency_key=key
    )
    assert conflicting.status_code == 409
    assert error_code(conflicting) == "idempotency_key_reuse"
    # The rejected attempt must not have reserved anything.
    assert stock_of(product_service, second_product["id"])["reserved"] == 0


# --------------------------------------------------------------------------- rejections


async def test_insufficient_stock_is_a_business_conflict(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-SHORT", stock=2, price="10.00")
    response = await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 5}])

    assert response.status_code == 409
    assert error_code(response) == "insufficient_stock"
    shortage = response.json()["error"]["details"]["shortage"]
    assert shortage[0]["sku"] == "ORD-SHORT"
    assert shortage[0]["requested"] == 5 and shortage[0]["available"] == 2
    assert stock_of(product_service, product["id"])["available"] == 2


async def test_unknown_product_is_a_404(
    client: httpx.AsyncClient, alice_headers: dict[str, str]
) -> None:
    response = await place_order(client, alice_headers, items=[{"product_id": str(uuid.uuid4()), "quantity": 1}])

    assert response.status_code == 404
    assert error_code(response) == "product_not_found"
    assert len(response.json()["error"]["details"]["missing_product_ids"]) == 1


async def test_inactive_product_cannot_be_ordered(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-INACTIVE", stock=5, active=False)
    response = await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 1}])

    assert response.status_code == 409
    assert error_code(response) == "product_not_available"
    assert response.json()["error"]["details"]["skus"] == ["ORD-INACTIVE"]


async def test_currency_mismatch_is_rejected(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-CURRENCY", stock=5)
    response = await place_order(
        client, alice_headers, items=[{"product_id": product["id"], "quantity": 1}], extra={"currency": "EUR"}
    )

    assert response.status_code == 409
    assert error_code(response) == "currency_mismatch"


async def test_duplicate_product_lines_are_rejected(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-DUPLICATE", stock=5)
    response = await place_order(
        client,
        alice_headers,
        items=[{"product_id": product["id"], "quantity": 1}, {"product_id": product["id"], "quantity": 2}],
    )

    assert response.status_code == 422
    assert error_code(response) == "validation_error"


# --------------------------------------------------------------------------- reads & authorization


async def test_orders_are_scoped_to_their_owner(
    client: httpx.AsyncClient,
    alice_headers: dict[str, str],
    bob_headers: dict[str, str],
    admin_headers: dict[str, str],
    product_service: ServiceClient,
) -> None:
    product = create_product(product_service, sku="ORD-SCOPE", stock=10)
    alice_order = (await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 1}])).json()
    bob_order = (await place_order(client, bob_headers, items=[{"product_id": product["id"], "quantity": 1}])).json()

    alice_list = await client.get(ORDERS, headers=alice_headers)
    assert [item["id"] for item in alice_list.json()["items"]] == [alice_order["id"]]

    bob_list = await client.get(ORDERS, headers=bob_headers)
    assert [item["id"] for item in bob_list.json()["items"]] == [bob_order["id"]]

    admin_list = await client.get(ORDERS, headers=admin_headers)
    assert {item["id"] for item in admin_list.json()["items"]} == {alice_order["id"], bob_order["id"]}
    assert admin_list.json()["meta"]["total"] == 2

    # Another customer's order is invisible (404, not 403: no existence leak).
    hidden = await client.get(f"{ORDERS}/{alice_order['id']}", headers=bob_headers)
    assert hidden.status_code == 404
    assert error_code(hidden) == "order_not_found"

    visible = await client.get(f"{ORDERS}/{alice_order['id']}", headers=admin_headers)
    assert visible.status_code == 200


async def test_list_filters_and_requires_auth(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    unauthenticated = await client.get(ORDERS)
    assert unauthenticated.status_code == 401

    product = create_product(product_service, sku="ORD-FILTER", stock=5)
    placed = (await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 1}])).json()

    pending = await client.get(f"{ORDERS}?status=pending&min_total=0", headers=alice_headers)
    assert pending.json()["meta"]["total"] == 1

    cancelled = await client.get(f"{ORDERS}?status=cancelled", headers=alice_headers)
    assert cancelled.json()["meta"]["total"] == 0

    numbered = await client.get(f"{ORDERS}?order_number={placed['order_number']}", headers=alice_headers)
    assert numbered.json()["items"][0]["id"] == placed["id"]

    invalid = await client.get(f"{ORDERS}?status=exploded", headers=alice_headers)
    assert invalid.status_code == 422
    assert error_code(invalid) == "invalid_order_status"


async def test_unknown_order_is_a_404(client: httpx.AsyncClient, alice_headers: dict[str, str]) -> None:
    response = await client.get(f"{ORDERS}/{uuid.uuid4()}", headers=alice_headers)
    assert response.status_code == 404
    assert error_code(response) == "order_not_found"


# --------------------------------------------------------------------------- cancellation


async def test_cancel_releases_stock_and_is_idempotent(
    client: httpx.AsyncClient, alice_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-CANCEL", stock=5, price="30.00")
    placed = (await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 2}])).json()
    assert stock_of(product_service, product["id"])["available"] == 3

    first = await client.post(f"{ORDERS}/{placed['id']}/cancel", json={"reason": "changed my mind"}, headers=alice_headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["order"]["status"] == "cancelled"
    assert body["stock_released"] is True
    assert body["release_outcome"] == "released"
    assert body["order"]["cancelled_at"] is not None
    assert body["order"]["cancel_reason"] == "changed my mind"
    assert stock_of(product_service, product["id"])["available"] == 5

    second = await client.post(f"{ORDERS}/{placed['id']}/cancel", json={}, headers=alice_headers)
    assert second.status_code == 200
    assert second.json()["stock_released"] is False
    assert second.json()["release_outcome"] == "already_cancelled"

    timeline = await client.get(f"{ORDERS}/{placed['id']}/timeline", headers=alice_headers)
    assert timeline.status_code == 200
    history = timeline.json()["history"]
    assert [entry["to_status"] for entry in history] == ["pending", "cancelled"]
    assert history[-1]["reason"] == "changed my mind"
    assert history[-1]["source"] == "api"


async def test_cancel_requires_ownership(
    client: httpx.AsyncClient, alice_headers: dict[str, str], bob_headers: dict[str, str], product_service: ServiceClient
) -> None:
    product = create_product(product_service, sku="ORD-CANCEL-AUTH", stock=3)
    placed = (await place_order(client, alice_headers, items=[{"product_id": product["id"], "quantity": 1}])).json()

    response = await client.post(f"{ORDERS}/{placed['id']}/cancel", json={}, headers=bob_headers)
    assert response.status_code == 404
    assert error_code(response) == "order_not_found"
    # Alice's stock is untouched.
    assert stock_of(product_service, product["id"])["reserved"] == 1
