"""Stock reservation ledger: two-phase commit, idempotency and concurrency."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

RESERVATIONS = "/products/reservations"


async def create_product(
    client: httpx.AsyncClient, admin_headers: dict[str, str], *, sku: str, stock: int, price: str = "10.00", active: bool = True
) -> dict[str, object]:
    response = await client.post(
        "/products",
        json={
            "sku": sku,
            "name": f"Widget {sku}",
            "description": "reservation test product",
            "category": "test-widgets",
            "price": price,
            "currency": "USD",
            "stock": stock,
            "active": active,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def available(client: httpx.AsyncClient, product_id: str) -> int:
    return (await client.get(f"/products/{product_id}/stock")).json()["available"]


async def test_reservation_requires_internal_token(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    product = await create_product(client, admin_headers, sku="SKU-RES-TOKEN", stock=5)
    response = await client.post(
        RESERVATIONS, json={"order_id": str(uuid.uuid4()), "items": [{"product_id": product["id"], "quantity": 1}]}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_internal_token"

    wrong = await client.post(
        RESERVATIONS,
        json={"order_id": str(uuid.uuid4()), "items": [{"product_id": product["id"], "quantity": 1}]},
        headers={"X-Internal-Token": "nope", "X-Source-Service": "test-harness"},
    )
    assert wrong.status_code == 401


async def test_reserve_is_idempotent_by_order_id(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str]
) -> None:
    product = await create_product(client, admin_headers, sku="SKU-RES-IDEM", stock=10, price="12.50")
    order_id = str(uuid.uuid4())
    payload = {"order_id": order_id, "items": [{"product_id": product["id"], "quantity": 3}]}

    first = await client.post(RESERVATIONS, json=payload, headers=internal_headers)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "active"
    assert body["total"] == "37.50"
    assert first.headers["X-Idempotent-Replay"] == "false"
    assert await available(client, product["id"]) == 7

    # A retry (same order, same lines) replays the original reservation instead of
    # double booking stock.
    replay = await client.post(RESERVATIONS, json=payload, headers=internal_headers)
    assert replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json()["order_id"] == order_id
    assert await available(client, product["id"]) == 7

    conflict = await client.post(
        RESERVATIONS,
        json={"order_id": order_id, "items": [{"product_id": product["id"], "quantity": 5}]},
        headers=internal_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "reservation_conflict"
    assert await available(client, product["id"]) == 7


async def test_shortage_is_rejected_atomically(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str]
) -> None:
    ok_product = await create_product(client, admin_headers, sku="SKU-RES-OK", stock=5)
    short_product = await create_product(client, admin_headers, sku="SKU-RES-SHORT", stock=1)

    response = await client.post(
        RESERVATIONS,
        json={
            "order_id": str(uuid.uuid4()),
            "items": [
                {"product_id": ok_product["id"], "quantity": 2},
                {"product_id": short_product["id"], "quantity": 4},
            ],
        },
        headers=internal_headers,
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "insufficient_stock"
    assert error["details"]["shortage"] == [
        {"product_id": short_product["id"], "sku": "SKU-RES-SHORT", "requested": 4, "available": 1}
    ]
    # All-or-nothing: the healthy line was not reserved either.
    assert await available(client, ok_product["id"]) == 5
    assert await available(client, short_product["id"]) == 1


async def test_reserve_unknown_or_inactive_product(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str]
) -> None:
    inactive = await create_product(client, admin_headers, sku="SKU-RES-INACTIVE", stock=5, active=False)

    missing = await client.post(
        RESERVATIONS,
        json={"order_id": str(uuid.uuid4()), "items": [{"product_id": str(uuid.uuid4()), "quantity": 1}]},
        headers=internal_headers,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "product_not_found"

    disabled = await client.post(
        RESERVATIONS,
        json={"order_id": str(uuid.uuid4()), "items": [{"product_id": inactive["id"], "quantity": 1}]},
        headers=internal_headers,
    )
    assert disabled.status_code == 409
    assert disabled.json()["error"]["code"] == "product_not_available"


async def test_release_and_commit_lifecycle(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str]
) -> None:
    product = await create_product(client, admin_headers, sku="SKU-RES-LIFE", stock=10, price="5.00")

    # release path -> stock comes back
    to_release = str(uuid.uuid4())
    assert (
        await client.post(
            RESERVATIONS,
            json={"order_id": to_release, "items": [{"product_id": product["id"], "quantity": 4}]},
            headers=internal_headers,
        )
    ).status_code == 201
    assert await available(client, product["id"]) == 6

    released = await client.post(f"{RESERVATIONS}/{to_release}/release", headers=internal_headers)
    assert released.status_code == 200
    assert released.json() == {"order_id": to_release, "status": "released", "released": True, "committed": False}
    assert await available(client, product["id"]) == 10

    again = await client.post(f"{RESERVATIONS}/{to_release}/release", headers=internal_headers)
    assert again.json()["status"] == "already_released"
    assert await available(client, product["id"]) == 10

    # commit path -> stock actually leaves the warehouse
    to_commit = str(uuid.uuid4())
    assert (
        await client.post(
            RESERVATIONS,
            json={"order_id": to_commit, "items": [{"product_id": product["id"], "quantity": 6}]},
            headers=internal_headers,
        )
    ).status_code == 201
    committed = await client.post(f"{RESERVATIONS}/{to_commit}/commit", headers=internal_headers)
    assert committed.status_code == 200
    assert committed.json()["status"] == "committed"
    stock = (await client.get(f"/products/{product['id']}/stock")).json()
    assert stock["stock"] == 4
    assert stock["reserved"] == 0
    assert stock["low_stock"] is True  # 4 <= default threshold of 5

    repeated = await client.post(f"{RESERVATIONS}/{to_commit}/commit", headers=internal_headers)
    assert repeated.json()["status"] == "already_committed"

    # a released reservation can never be committed afterwards
    refused = await client.post(f"{RESERVATIONS}/{to_release}/commit", headers=internal_headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "reservation_already_released"


async def test_release_unknown_reservation_is_tolerated(
    client: httpx.AsyncClient, internal_headers: dict[str, str]
) -> None:
    order_id = str(uuid.uuid4())
    released = await client.post(f"{RESERVATIONS}/{order_id}/release", headers=internal_headers)
    assert released.status_code == 200
    assert released.json()["status"] == "not_found"

    fetched = await client.get(f"{RESERVATIONS}/{order_id}", headers=internal_headers)
    assert fetched.status_code == 404
    assert fetched.json()["error"]["code"] == "reservation_not_found"


async def test_concurrent_reservations_never_oversell(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str]
) -> None:
    """20 buyers race for the last 5 units; row locks must prevent overselling."""
    product = await create_product(client, admin_headers, sku="SKU-RES-RACE", stock=5)

    async def reserve() -> httpx.Response:
        return await client.post(
            RESERVATIONS,
            json={"order_id": str(uuid.uuid4()), "items": [{"product_id": product["id"], "quantity": 1}]},
            headers=internal_headers,
        )

    responses = await asyncio.gather(*(reserve() for _ in range(20)))
    accepted = [response for response in responses if response.status_code == 201]
    rejected = [response for response in responses if response.status_code == 409]

    assert len(accepted) == 5, [response.text for response in responses if response.status_code not in (201, 409)]
    assert len(rejected) == 15
    assert all(response.json()["error"]["code"] == "insufficient_stock" for response in rejected)
    assert await available(client, product["id"]) == 0


async def test_batch_lookup_for_other_services(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str]
) -> None:
    first = await create_product(client, admin_headers, sku="SKU-LOOKUP-A", stock=3, price="1.25")
    second = await create_product(client, admin_headers, sku="SKU-LOOKUP-B", stock=0, price="2.50")

    response = await client.post(
        "/products/lookup", json={"ids": [first["id"], second["id"], str(uuid.uuid4())]}, headers=internal_headers
    )
    assert response.status_code == 200, response.text
    found = {item["id"]: item for item in response.json()}
    assert set(found) == {first["id"], second["id"]}
    assert found[first["id"]]["price"] == "1.25"
    assert found[second["id"]]["available"] == 0

    unauthenticated = await client.post("/products/lookup", json={"ids": [first["id"]]})
    assert unauthenticated.status_code == 401


@pytest.mark.parametrize("quantity", [0, -3, 10_001])
async def test_reservation_quantity_validation(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str], quantity: int
) -> None:
    product = await create_product(client, admin_headers, sku=f"SKU-RES-Q{quantity}", stock=1)
    response = await client.post(
        RESERVATIONS,
        json={"order_id": str(uuid.uuid4()), "items": [{"product_id": product["id"], "quantity": quantity}]},
        headers=internal_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
