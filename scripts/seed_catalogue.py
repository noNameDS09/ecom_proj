#!/usr/bin/env python3
"""Seed the catalogue through the public APIs (standard library only).

Logs in as the bootstrap admin and creates a small demo catalogue. SKUs that
already exist get their imagery backfilled (PATCH) when it is missing, so
re-running the script keeps an existing catalogue looking current.

    python scripts/seed_catalogue.py

Override endpoints/credentials with env vars:
    PRODUCT_SERVICE_URL, USER_SERVICE_URL, ADMIN_EMAIL, ADMIN_PASSWORD
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PRODUCT_SERVICE_URL = os.environ.get("PRODUCT_SERVICE_URL", "http://localhost:8001").rstrip("/")
USER_SERVICE_URL = os.environ.get("USER_SERVICE_URL", "http://localhost:8002").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin-password-1")

# Prices deliberately avoid ...13 cents: the simulated provider declines those.
PRODUCTS: list[dict[str, object]] = [
    {
        "sku": "SKU-KEYBOARD-001",
        "name": "Mechanical Keyboard",
        "description": "Hot-swappable switches, PBT keycaps, USB-C.",
        "category": "peripherals",
        "price": "89.90",
        "stock": 25,
    },
    {
        "sku": "SKU-MOUSE-002",
        "name": "Wireless Mouse",
        "description": "Ergonomic shape, 4000 dpi sensor, 6-month battery.",
        "category": "peripherals",
        "price": "29.90",
        "stock": 60,
    },
    {
        "sku": "SKU-HEADSET-003",
        "name": "Noise-cancelling Headphones",
        "description": "Over-ear ANC with 30-hour battery life.",
        "category": "audio",
        "price": "149.00",
        "stock": 18,
    },
    {
        "sku": "SKU-MONITOR-004",
        "name": "27-inch 4K Monitor",
        "description": "IPS panel, 60 Hz, USB-C with 90 W power delivery.",
        "category": "monitors",
        "price": "349.00",
        "stock": 12,
    },
    {
        "sku": "SKU-WEBCAM-005",
        "name": "Full HD Webcam",
        "description": "1080p60 with auto-framing and dual mics.",
        "category": "peripherals",
        "price": "59.00",
        "stock": 30,
    },
    {
        "sku": "SKU-USBHUB-006",
        "name": "USB-C Hub 8-in-1",
        "description": "HDMI 4K, 2x USB-A, USB-C data, SD/microSD, 100 W passthrough.",
        "category": "accessories",
        "price": "44.50",
        "stock": 40,
    },
    {
        "sku": "SKU-STAND-007",
        "name": "Aluminium Laptop Stand",
        "description": "Adjustable height, fits 11-17 inch laptops.",
        "category": "accessories",
        "price": "34.90",
        "stock": 35,
    },
    {
        "sku": "SKU-SPEAKER-008",
        "name": "Bluetooth Speaker",
        "description": "IPX7 waterproof, 12-hour playtime.",
        "category": "audio",
        "price": "69.90",
        "stock": 22,
    },
    {
        "sku": "SKU-LAMP-009",
        "name": "Smart Desk Lamp",
        "description": "Tunable white, app control, auto-brightness.",
        "category": "smart home",
        "price": "49.00",
        "stock": 28,
    },
    {
        "sku": "SKU-DOCK-010",
        "name": "Thunderbolt Dock",
        "description": "Dual 4K displays, 96 W charging, 10 GbE.",
        "category": "accessories",
        "price": "199.00",
        "stock": 8,
    },
    {
        "sku": "SKU-MICRO-011",
        "name": "USB Microphone",
        "description": "Cardioid condenser with shock mount and headphone jack.",
        "category": "audio",
        "price": "79.00",
        "stock": 16,
    },
    {
        "sku": "SKU-SSD-012",
        "name": "Portable SSD 1 TB",
        "description": "1050 MB/s over USB-C, shock-resistant shell.",
        "category": "storage",
        "price": "119.00",
        "stock": 14,
    },
]

# Deterministic placeholder photography (picsum). The storefront falls back to
# a generated tile whenever an image cannot be loaded.
for _product in PRODUCTS:
    _product["image_url"] = f"https://picsum.photos/seed/{str(_product['sku']).lower()}/800/600"


def request(method: str, url: str, *, token: str | None = None, payload: dict[str, object] | None = None) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"content-type": "application/json"} if data is not None else {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    http_request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(http_request, timeout=15) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode(errors="replace")}


def find_by_sku(token: str, sku: str) -> dict[str, object] | None:
    status, page = request("GET", f"{PRODUCT_SERVICE_URL}/products?q={urllib.parse.quote(sku)}&limit=5", token=token)
    if status != 200 or not isinstance(page, dict):
        return None
    for item in page.get("items", []):
        if isinstance(item, dict) and item.get("sku") == sku:
            return item
    return None


def main() -> int:
    status, login = request("POST", f"{USER_SERVICE_URL}/auth/login", payload={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if status != 200 or not isinstance(login, dict):
        print(f"admin login failed ({status}): {login}", file=sys.stderr)
        return 1
    token = str(login["tokens"]["access_token"])  # type: ignore[index]

    created = updated = skipped = failed = 0
    for product in PRODUCTS:
        status, body = request("POST", f"{PRODUCT_SERVICE_URL}/products", token=token, payload=product)
        if status == 201:
            created += 1
            print(f"  + {product['sku']}: {product['name']}")
            continue
        if status != 409:
            failed += 1
            print(f"  ! {product['sku']}: failed ({status}): {body}", file=sys.stderr)
            continue

        existing = find_by_sku(token, str(product["sku"]))
        if existing is None or existing.get("image_url") == product["image_url"]:
            skipped += 1
            print(f"  = {product['sku']}: already exists, skipping")
            continue

        patch_status, patch_body = request(
            "PATCH",
            f"{PRODUCT_SERVICE_URL}/products/{existing['id']}",
            token=token,
            payload={"image_url": product["image_url"]},
        )
        if patch_status == 200:
            updated += 1
            print(f"  ~ {product['sku']}: image backfilled")
        else:
            failed += 1
            print(f"  ! {product['sku']}: image backfill failed ({patch_status}): {patch_body}", file=sys.stderr)

    print(f"done: {created} created, {updated} updated, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
