"""Payment service domain metrics."""

from __future__ import annotations

from common import metrics

payments_total = metrics.domain_counter("payment_attempts_total", "Payment attempts by outcome.", ("outcome", "source"))
payment_amount_total = metrics.domain_counter("payment_amount_total", "Value of payments by status.", ("currency", "status"))
idempotent_replays_total = metrics.domain_counter(
    "payment_idempotent_replays_total", "Charges answered from the existing payment row.", ("endpoint",)
)
