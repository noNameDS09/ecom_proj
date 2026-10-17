"""Order service domain metrics."""

from __future__ import annotations

from common import metrics

orders_created_total = metrics.domain_counter("order_created_total", "Orders created.", ("outcome",))
orders_cancelled_total = metrics.domain_counter("order_cancelled_total", "Orders cancelled.", ("outcome",))
orders_fulfilled_total = metrics.domain_counter("order_fulfilled_total", "Orders fulfilled.", ("outcome",))
orders_status_changes_total = metrics.domain_counter(
    "order_status_changes_total", "Order status transitions.", ("from_status", "to_status", "source")
)
order_amount_total = metrics.domain_counter("order_amount_total", "Total value of created orders.", ("currency",))
idempotent_replays_total = metrics.domain_counter(
    "order_idempotent_replays_total", "Requests answered from the idempotency ledger.", ("endpoint",)
)
saga_outcomes_total = metrics.domain_counter(
    "order_saga_outcomes_total", "Payment saga outcomes processed for orders.", ("outcome",)
)
compensations_total = metrics.domain_counter(
    "order_compensations_total", "Compensating actions (stock release) executed.", ("reason", "outcome")
)
