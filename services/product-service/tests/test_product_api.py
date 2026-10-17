"""Catalogue CRUD, filtering, optimistic locking and observability endpoints."""

from __future__ import annotations

import httpx

BASE = "/products"


def new_product(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sku": "SKU-TEST-001",
        "name": "Test Widget",
        "description": "A widget used by the test suite",
        "category": "test-widgets",
        "price": "19.99",
        "currency": "usd",
        "stock": 10,
        "active": True,
    }
    payload.update(overrides)
    return payload


async def test_create_requires_admin_role(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    unauthenticated = await client.post(BASE, json=new_product())
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "unauthorized"
    assert unauthenticated.headers["X-Request-ID"]

    customer = {"Authorization": "Bearer " + _customer_token()}
    forbidden = await client.post(BASE, json=new_product(), headers=customer)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    created = await client.post(BASE, json=new_product(), headers=admin_headers)
    assert created.status_code == 201
    body = created.json()
    assert body["sku"] == "SKU-TEST-001"
    assert body["currency"] == "USD"
    assert body["price"] == "19.99"  # money is serialised as an exact string
    assert body["available"] == 10
    assert body["version"] == 1


async def test_create_validates_payload(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    invalid_price = await client.post(BASE, json=new_product(price="-1.00"), headers=admin_headers)
    assert invalid_price.status_code == 422
    assert invalid_price.json()["error"]["code"] == "validation_error"
    details = invalid_price.json()["error"]["details"]["errors"]
    assert any(item["field"] == "price" for item in details)

    unknown_field = await client.post(BASE, json=new_product(bogus="x"), headers=admin_headers)
    assert unknown_field.status_code == 422

    bad_sku = await client.post(BASE, json=new_product(sku="bad sku!"), headers=admin_headers)
    assert bad_sku.status_code == 422


async def test_duplicate_sku_is_a_conflict(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    first = await client.post(BASE, json=new_product(), headers=admin_headers)
    assert first.status_code == 201
    second = await client.post(BASE, json=new_product(name="Another"), headers=admin_headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "sku_already_exists"
    assert second.json()["error"]["details"]["sku"] == "SKU-TEST-001"


async def test_get_product_and_not_found(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    created = (await client.post(BASE, json=new_product(), headers=admin_headers)).json()

    fetched = await client.get(f"{BASE}/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]

    missing = await client.get("/products/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "product_not_found"

    malformed = await client.get("/products/not-a-uuid")
    assert malformed.status_code == 422


async def test_seeded_catalogue_listing_filters_and_pagination(client: httpx.AsyncClient) -> None:
    page = await client.get(BASE, params={"limit": 2})
    assert page.status_code == 200
    body = page.json()
    assert len(body["items"]) == 2
    assert body["meta"]["total"] >= 6
    assert body["meta"]["has_more"] is True

    second = (await client.get(BASE, params={"limit": 2, "offset": 2})).json()
    assert {item["id"] for item in body["items"]}.isdisjoint({item["id"] for item in second["items"]})

    peripherals = (await client.get(BASE, params={"category": "peripherals"})).json()
    assert peripherals["meta"]["total"] == 2
    assert all(item["category"] == "peripherals" for item in peripherals["items"])

    search = (await client.get(BASE, params={"q": "keyboard"})).json()
    assert search["meta"]["total"] == 1
    assert search["items"][0]["sku"] == "SKU-KEYBOARD-001"

    in_stock = (await client.get(BASE, params={"in_stock": True})).json()
    assert all(item["available"] > 0 for item in in_stock["items"])
    assert all(item["sku"] != "SKU-DOCK-001" for item in in_stock["items"])

    expensive = (await client.get(BASE, params={"min_price": "100"})).json()
    assert all(float(item["price"]) >= 100 for item in expensive["items"])
    assert expensive["meta"]["total"] == 3  # 129.00, 199.00, 429.99

    cheap = (await client.get(BASE, params={"max_price": "60"})).json()
    assert cheap["meta"]["total"] == 2  # 45.00 (inactive), 59.50
    assert all(float(item["price"]) <= 60 for item in cheap["items"])

    by_price = (await client.get(BASE, params={"sort": "price"})).json()
    prices = [float(item["price"]) for item in by_price["items"]]
    assert prices == sorted(prices)

    ids = [item["id"] for item in body["items"]]
    selected = (await client.get(BASE, params={"ids": ",".join(ids)})).json()
    assert {item["id"] for item in selected["items"]} == set(ids)

    bad_ids = await client.get(BASE, params={"ids": "nope"})
    assert bad_ids.status_code == 409
    assert bad_ids.json()["error"]["code"] == "invalid_product_id"


async def test_patch_uses_patch_semantics_and_optimistic_locking(
    client: httpx.AsyncClient, admin_headers: dict[str, str], internal_headers: dict[str, str]
) -> None:
    created = (await client.post(BASE, json=new_product(), headers=admin_headers)).json()

    patched = await client.patch(f"{BASE}/{created['id']}", json={"name": "Renamed Widget"}, headers=admin_headers)
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed Widget"
    assert patched.json()["price"] == created["price"]
    assert patched.json()["version"] == created["version"] + 1

    empty_patch = await client.patch(f"{BASE}/{created['id']}", json={}, headers=admin_headers)
    assert empty_patch.status_code == 200
    assert empty_patch.json()["version"] == patched.json()["version"]

    stale = await client.patch(
        f"{BASE}/{created['id']}", json={"name": "Nope"}, headers={**admin_headers, "If-Match": "1"}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_mismatch"
    assert stale.json()["error"]["details"] == {"expected_version": 1, "actual_version": 2}

    fresh = await client.patch(
        f"{BASE}/{created['id']}", json={"name": "Fresh"}, headers={**admin_headers, "If-Match": "2"}
    )
    assert fresh.status_code == 200

    below_reserved = await client.post(
        f"{BASE}/reservations",
        json={"order_id": "11111111-1111-1111-1111-111111111111", "items": [{"product_id": created["id"], "quantity": 4}]},
        headers=internal_headers,
    )
    assert below_reserved.status_code == 201, below_reserved.text
    refused = await client.patch(f"{BASE}/{created['id']}", json={"stock": 1}, headers=admin_headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "stock_below_reserved"


async def test_soft_delete_hides_product_and_frees_the_sku(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    created = (await client.post(BASE, json=new_product(), headers=admin_headers)).json()

    deleted = await client.delete(f"{BASE}/{created['id']}", headers=admin_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    assert (await client.get(f"{BASE}/{created['id']}")).status_code == 404
    listing = (await client.get(BASE, params={"q": "SKU-TEST-001"})).json()
    assert created["id"] not in {item["id"] for item in listing["items"]}

    with_deleted = (await client.get(BASE, params={"q": "SKU-TEST-001", "include_deleted": True})).json()
    assert created["id"] in {item["id"] for item in with_deleted["items"]}

    # The partial unique index only covers live rows, so the SKU can be reused.
    reused = await client.post(BASE, json=new_product(name="Reused SKU"), headers=admin_headers)
    assert reused.status_code == 201


async def test_read_cache_is_used_and_invalidated_on_write(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    created = (await client.post(BASE, json=new_product(), headers=admin_headers)).json()

    for _ in range(3):
        assert (await client.get(f"{BASE}/{created['id']}")).status_code == 200
    stats = (await client.get("/_catalogue/cache")).json()
    assert stats["hits"] >= 2, stats

    updated = await client.patch(f"{BASE}/{created['id']}", json={"name": "Cache Busting Widget"}, headers=admin_headers)
    assert updated.status_code == 200
    refetched = (await client.get(f"{BASE}/{created['id']}")).json()
    assert refetched["name"] == "Cache Busting Widget"
    assert refetched["version"] == 2


async def test_health_endpoints_and_metrics(client: httpx.AsyncClient) -> None:
    assert (await client.get("/live")).json()["status"] == "alive"

    ready = await client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"]["status"] == "ok"

    version = (await client.get("/version")).json()
    assert version["service"] == "product-service"
    assert version["environment"] == "test"

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "service_http_requests_total" in metrics.text
    assert "service_readiness_status" in metrics.text

    runtime = (await client.get("/_runtime")).json()
    assert runtime["ready"] is True


async def test_product_images_are_optional_and_validated(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    with_image = await client.post(
        BASE, json=new_product(sku="SKU-IMG-001", image_url="https://cdn.example.com/widget.png"), headers=admin_headers
    )
    assert with_image.status_code == 201, with_image.text
    assert with_image.json()["image_url"] == "https://cdn.example.com/widget.png"

    without_image = await client.post(BASE, json=new_product(sku="SKU-IMG-002"), headers=admin_headers)
    assert without_image.status_code == 201
    assert without_image.json()["image_url"] is None

    patched = await client.patch(
        f"{BASE}/{without_image.json()['id']}", json={"image_url": "https://cdn.example.com/backfilled.png"}, headers=admin_headers
    )
    assert patched.status_code == 200
    assert patched.json()["image_url"] == "https://cdn.example.com/backfilled.png"

    for index, bad_url in enumerate(("javascript:alert(1)", "not-a-url", "ftp://example.com/a.png")):
        rejected = await client.post(
            BASE, json=new_product(sku=f"SKU-BADURL-{index}", image_url=bad_url), headers=admin_headers
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "validation_error"


async def test_category_facets_list_active_categories_with_counts(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    for sku, category, active in (
        ("SKU-FACET-001", "facet-audio", True),
        ("SKU-FACET-002", "facet-audio", True),
        ("SKU-FACET-003", "facet-keyboards", True),
        ("SKU-FACET-004", "facet-legacy", False),
    ):
        created = await client.post(BASE, json=new_product(sku=sku, category=category, active=active), headers=admin_headers)
        assert created.status_code == 201, created.text

    facets = await client.get(f"{BASE}/categories")
    assert facets.status_code == 200
    counts = {item["name"]: item["product_count"] for item in facets.json()["items"]}
    # The base migration seeds a few demo products, so assert on the fixture
    # categories only; "facet-legacy" must be absent because it is inactive.
    assert counts["facet-audio"] == 2
    assert counts["facet-keyboards"] == 1
    assert "facet-legacy" not in counts


def _customer_token() -> str:
    from tests.support.harness import TEST_JWT_SECRET, issue_token

    return issue_token(roles=("customer",), secret=TEST_JWT_SECRET)
