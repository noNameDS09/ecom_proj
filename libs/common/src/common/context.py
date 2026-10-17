"""Request-scoped context propagation.

Every log record, outgoing HTTP call and published event carries the same
identifiers so a single user action can be followed across services:

    X-Request-ID      unique per incoming HTTP request (generated if absent)
    X-Correlation-ID  unique per business transaction, propagated downstream
    principal_id      authenticated user id, when the request is authenticated
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

REQUEST_ID_HEADER: Final = "X-Request-ID"
CORRELATION_ID_HEADER: Final = "X-Correlation-ID"

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)
_principal_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("principal_id", default=None)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:20]}"


def new_correlation_id() -> str:
    return f"cor_{uuid.uuid4().hex[:20]}"


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:20]}"


def get_request_id() -> str | None:
    return _request_id.get()


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_principal_id() -> str | None:
    return _principal_id.get()


def set_request_id(value: str | None) -> None:
    _request_id.set(value)


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def set_principal_id(value: str | None) -> None:
    _principal_id.set(value)


def ensure_correlation_id(value: str | None = None) -> str:
    """Return the current correlation id, creating one when absent."""
    existing = _correlation_id.get()
    if existing:
        return existing
    created = value or new_correlation_id()
    _correlation_id.set(created)
    return created


def correlation_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers used for outbound HTTP calls and Kafka message headers."""
    headers: dict[str, str] = {}
    if (rid := _request_id.get()) is not None:
        headers[REQUEST_ID_HEADER] = rid
    if (cid := _correlation_id.get()) is not None:
        headers[CORRELATION_ID_HEADER] = cid
    if extra:
        headers.update(extra)
    return headers


def snapshot() -> dict[str, Any]:
    """Context values for structured log records."""
    values: dict[str, Any] = {}
    if (rid := _request_id.get()) is not None:
        values["request_id"] = rid
    if (cid := _correlation_id.get()) is not None:
        values["correlation_id"] = cid
    if (pid := _principal_id.get()) is not None:
        values["principal_id"] = pid
    return values


@contextmanager
def bind_context(
    *,
    request_id: str | None = None,
    correlation_id: str | None = None,
    principal_id: str | None = None,
) -> Iterator[None]:
    """Bind context values for the duration of the block (used in consumers/workers)."""
    tokens: list[tuple[contextvars.ContextVar[str | None], contextvars.Token[str | None]]] = []
    for var, value in (
        (_request_id, request_id),
        (_correlation_id, correlation_id),
        (_principal_id, principal_id),
    ):
        if value is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
