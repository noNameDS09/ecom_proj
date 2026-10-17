"""User service domain metrics."""

from __future__ import annotations

from common import metrics

registrations_total = metrics.domain_counter("user_registrations_total", "Successful registrations.", ("outcome",))
logins_total = metrics.domain_counter("user_logins_total", "Login attempts.", ("outcome",))
token_operations_total = metrics.domain_counter(
    "user_token_operations_total", "Access/refresh token operations.", ("operation", "outcome")
)
refresh_reuse_detected_total = metrics.domain_counter(
    "user_refresh_reuse_detected_total", "Refresh tokens replayed after rotation (possible token theft)."
)
account_lockouts_total = metrics.domain_counter("user_account_lockouts_total", "Accounts locked after repeated failures.")
active_refresh_tokens_gauge = metrics.domain_gauge(
    "user_active_refresh_tokens", "Non-revoked, unexpired refresh tokens (refreshed hourly by the reaper)."
)
