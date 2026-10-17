"""Product service domain metrics (registered on the shared registry)."""

from __future__ import annotations

from common import metrics

products_created_total = metrics.domain_counter("product_created_total", "Products created.")
products_updated_total = metrics.domain_counter("product_updated_total", "Products updated.")
products_deleted_total = metrics.domain_counter("product_deleted_total", "Products soft deleted.")
catalogue_cache_events_total = metrics.domain_counter(
    "product_catalogue_cache_events_total", "Catalogue cache lookups.", ("event",)
)
stock_reserved_total = metrics.domain_counter(
    "product_stock_reserved_total", "Stock reservation attempts.", ("outcome",)
)
stock_reservations_released_total = metrics.domain_counter(
    "product_stock_reservations_released_total", "Reservations released.", ("outcome",)
)
stock_reservations_committed_total = metrics.domain_counter(
    "product_stock_reservations_committed_total", "Reservations committed.", ("outcome",)
)
stock_units_changed_total = metrics.domain_counter(
    "product_stock_units_changed_total", "Stock units reserved/released/committed.", ("operation",)
)
