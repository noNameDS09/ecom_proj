"""ASGI middleware: request context, access logs and RED metrics.

Implemented as a raw ASGI middleware (not ``BaseHTTPMiddleware``) so the
request context lives in the same task as the endpoint - contextvars set here
are visible to every log line and outbound call made while serving the request.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from common import context, metrics
from common.logging import get_logger

logger = get_logger("common.access")

# Routes excluded from the access log to keep container logs readable.
_QUIET_PATHS = frozenset({"/health", "/live", "/ready", "/metrics"})


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, *, service_name: str, access_log: bool = True, metrics_enabled: bool = True) -> None:
        self.app = app
        self.service_name = service_name
        self.access_log = access_log
        self.metrics_enabled = metrics_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan / websocket
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(context.REQUEST_ID_HEADER) or context.new_request_id()
        correlation_id = headers.get(context.CORRELATION_ID_HEADER) or request_id

        rid_token = context._request_id.set(request_id)
        cid_token = context._correlation_id.set(correlation_id)
        # Also expose them on the ASGI state so tests/handlers can read them cheaply.
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        scope["state"]["correlation_id"] = correlation_id

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        started = time.perf_counter()
        status_code = 500
        response_bytes = 0
        error_code: str | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_bytes, error_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers[context.REQUEST_ID_HEADER] = request_id
                response_headers[context.CORRELATION_ID_HEADER] = correlation_id
                error_code = response_headers.get("X-Error-Code")
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b"") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # pure ASGI middleware: nothing upstream turns this into a response
            error_code = type(exc).__name__
            self._record(method, scope, path, 500, time.perf_counter() - started)
            logger.exception("request crashed", extra={"method": method, "path": path, "duration_ms": _ms(started)})
            raise
        else:
            duration = time.perf_counter() - started
            self._record(method, scope, path, status_code, duration)
            if self.access_log and path not in _QUIET_PATHS:
                logger.info(
                    "http request",
                    extra={
                        "method": method,
                        "path": path,
                        "route": _route(scope, path),
                        "status": status_code,
                        "duration_ms": round(duration * 1000, 2),
                        "response_bytes": response_bytes,
                        **({"error_code": error_code} if error_code else {}),
                    },
                )
        finally:
            context._request_id.reset(rid_token)
            context._correlation_id.reset(cid_token)

    def _record(self, method: str, scope: Scope, path: str, status: int, duration: float) -> None:
        if not self.metrics_enabled:
            return
        route = _route(scope, path)
        metrics.http_requests_total.labels(self.service_name, method, route, str(status)).inc()
        metrics.http_request_duration_seconds.labels(self.service_name, method, route).observe(duration)
        if status >= 500:
            metrics.http_errors_total.labels(self.service_name, route, str(status)).inc()


def _route(scope: Scope, path: str) -> str:
    """Use the matched route template to keep metric cardinality bounded."""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    if scope.get("path_params") is None and path not in _QUIET_PATHS:
        # Unmatched path (404): collapse ids to avoid unbounded label values.
        parts = [part for part in path.split("/") if part]
        collapsed = [p if (len(p) <= 24 and not _looks_like_id(p)) else ":id" for p in parts]
        return "/" + "/".join(collapsed)
    return path


def _looks_like_id(value: str) -> bool:
    if value.count("-") == 4 and len(value) == 36:
        return True
    return len(value) > 20 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def add_middleware(app: Any, *, service_name: str, access_log: bool = True, metrics_enabled: bool = True) -> None:
    app.add_middleware(
        ObservabilityMiddleware,
        service_name=service_name,
        access_log=access_log,
        metrics_enabled=metrics_enabled,
    )


__all__ = ["ObservabilityMiddleware", "add_middleware"]
