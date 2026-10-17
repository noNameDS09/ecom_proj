"""Product service settings (``PRODUCT_SERVICE_*`` env vars take precedence)."""

from __future__ import annotations

from common.config import Settings


class ProductServiceSettings(Settings):
    service_name: str = "product-service"
    service_version: str = "0.1.0"
    port: int = 8001
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/product_db"

    # Catalogue read cache (per replica, short TTL, invalidated on write).
    cache_ttl_seconds: float = 15.0
    cache_max_entries: int = 2048

    default_currency: str = "USD"
    low_stock_threshold: int = 5
    max_items_per_reservation: int = 100
