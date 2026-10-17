"""Resilient service-to-service HTTP client.

Implements the four patterns the platform relies on for synchronous calls:

* **Timeout budget** - bounded connect/read/write/pool timeouts on every call.
* **Bounded retries** - exponential backoff with jitter, only for idempotent
  requests (or when the caller explicitly says a retry is safe).
* **Circuit breaker** - per target, so a sick dependency is not hammered while
  it recovers; business answers (404/409/422) never trip it.
* **Bulkhead** - a semaphore caps in-flight calls so one slow dependency cannot
  exhaust the event loop / connection pool of the caller.

Failures are translated into the platform's error types so callers never have to
know about httpx.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from common import context, metrics
from common.errors import AppError, DependencyUnavailableError, UpstreamError, UpstreamTimeoutError
from common.logging import get_logger

logger = get_logger("common.http")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# Downstream statuses that mean "try again": a transient overload or gateway error.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Downstream statuses that are legitimate business answers, not infrastructure failures.
BREAKER_NEUTRAL_STATUSES = frozenset({400, 401, 403, 404, 409, 410, 412, 422})


@dataclass(frozen=True, slots=True)
class TimeoutBudget:
    connect: float = 2.0
    read: float = 5.0
    write: float = 5.0
    pool: float = 2.0

    def to_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect, read=self.read, write=self.write, pool=self.pool
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3  # total attempts, not retries
    base_delay: float = 0.15
    max_delay: float = 2.0
    jitter: float = 0.5

    def delay_for(self, attempt: int) -> float:
        """Full-jitter exponential backoff for the given 1-based attempt."""
        exponential = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return exponential * (1 - self.jitter * random.random())


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 5
    reset_timeout: float = 20.0
    half_open_successes: int = 2


class CircuitBreaker:
    """Minimal circuit breaker (closed -> open -> half-open -> closed)."""

    def __init__(self, name: str, service_name: str, policy: CircuitBreakerPolicy | None = None) -> None:
        self.name = name
        self.service_name = service_name
        self.policy = policy or CircuitBreakerPolicy()
        self._state = "closed"
        self._failures = 0
        self._half_open_successes = 0
        self._half_open_inflight = 0
        self._opened_at = 0.0
        self.opened_total = 0
        self.last_error: str | None = None

    @property
    def state(self) -> str:
        if self._state == "open" and self._reset_deadline_passed():
            return "half_open"
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == "open" and not self._reset_deadline_passed()

    def _reset_deadline_passed(self) -> bool:
        return time.monotonic() >= self._opened_at + self.policy.reset_timeout

    def allow(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if not self._reset_deadline_passed():
                return False
            self._transition("half_open")
            self._half_open_successes = 0
            self._half_open_inflight = 0
        # half_open: let a single probe through at a time
        if self._half_open_inflight >= 1:
            return False
        self._half_open_inflight += 1
        return True

    def on_success(self) -> None:
        if self._state == "half_open":
            self._half_open_inflight = max(0, self._half_open_inflight - 1)
            self._half_open_successes += 1
            if self._half_open_successes >= self.policy.half_open_successes:
                self._transition("closed")
                self._failures = 0
                logger.info("circuit breaker closed", extra={"target": self.name})
        elif self._state == "closed":
            self._failures = 0
        self._publish_metric()

    def on_failure(self, error: str | None = None) -> None:
        self.last_error = error
        if self._state == "half_open":
            self._half_open_inflight = max(0, self._half_open_inflight - 1)
            self._open()
            return
        if self._state == "open":
            self._opened_at = time.monotonic()
            self._publish_metric()
            return
        self._failures += 1
        if self._failures >= self.policy.failure_threshold:
            self._open()
        else:
            self._publish_metric()

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = time.monotonic()
        self.opened_total += 1
        self._publish_metric()
        logger.error(
            "circuit breaker opened",
            extra={
                "target": self.name,
                "failures": self._failures,
                "reset_in_seconds": self.policy.reset_timeout,
                "last_error": self.last_error,
            },
        )

    def _transition(self, state: str) -> None:
        self._state = state
        self._publish_metric()

    def _publish_metric(self) -> None:
        metrics.circuit_breaker_state.labels(self.service_name, self.name).set(1 if self.is_open else 0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "target": self.name,
            "state": self.state,
            "failures": self._failures,
            "opened_total": self.opened_total,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class CallResult:
    response: httpx.Response
    attempts: int
    duration_ms: float


class HttpClient:
    """Small, opinionated wrapper around ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        service_name: str,
        timeouts: TimeoutBudget | None = None,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreakerPolicy | None = None,
        max_concurrency: int = 32,
        default_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        live_path: str = "/live",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.service_name = service_name
        self.timeouts = timeouts or TimeoutBudget()
        self.retry = retry or RetryPolicy()
        self.breaker = CircuitBreaker(name, service_name, breaker)
        self.live_path = live_path
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeouts.to_httpx(),
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=max_concurrency * 2, max_keepalive_connections=max_concurrency),
            headers={
                "user-agent": f"{service_name}/1.0",
                "x-source-service": service_name,
                **(default_headers or {}),
            },
        )
        self._closed = False
        self.requests_total = 0
        self.failures_total = 0
        self.retries_total = 0

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.breaker._publish_metric()
        logger.info("http client ready", extra={"target": self.name, "base_url": self.base_url})

    async def close(self) -> None:
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def healthy(self) -> None:
        """Readiness probe - raises when the dependency is not reachable."""
        try:
            response = await self._client.get(
                self.live_path,
                headers=context.correlation_headers(),
                timeout=httpx.Timeout(connect=1.0, read=2.0, write=1.0, pool=1.0),
            )
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(f"{self.name} unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise DependencyUnavailableError(f"{self.name} is unhealthy (status {response.status_code})")

    # ------------------------------------------------------------------ calls

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        retry_safe: bool | None = None,
        expected: Sequence[int] | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        method = method.upper()
        safe = SAFE_METHODS.__contains__(method) if retry_safe is None else retry_safe
        attempts = self.retry.attempts if safe else 1
        request_headers = {**context.correlation_headers(), **(headers or {})}
        expected_statuses = set(expected) if expected is not None else None
        last_error: AppError | None = None

        for attempt in range(1, attempts + 1):
            if not self.breaker.allow():
                self.failures_total += 1
                metrics.upstream_requests_total.labels(self.service_name, self.name, "circuit_open").inc()
                raise DependencyUnavailableError(
                    f"{self.name} is unavailable (circuit breaker open)",
                    code="upstream_circuit_open",
                    details={"dependency": self.name, "state": self.breaker.state, "last_error": self.breaker.last_error},
                )

            started = time.perf_counter()
            self.requests_total += 1
            try:
                async with self._semaphore:
                    response = await self._client.request(method, path, params=params, json=json, headers=request_headers)
            except httpx.TimeoutException as exc:
                self.breaker.on_failure(type(exc).__name__)
                last_error = UpstreamTimeoutError(
                    f"{self.name} timed out after {self.timeouts.read}s",
                    details={"dependency": self.name, "path": path, "timeout_seconds": self.timeouts.read},
                )
                outcome = "timeout"
            except httpx.HTTPError as exc:
                self.breaker.on_failure(type(exc).__name__)
                last_error = DependencyUnavailableError(
                    f"{self.name} is unreachable ({type(exc).__name__})",
                    details={"dependency": self.name, "path": path, "reason": str(exc)},
                )
                outcome = "transport_error"
            else:
                duration = time.perf_counter() - started
                status = response.status_code
                if status in BREAKER_NEUTRAL_STATUSES:
                    self.breaker.on_success()
                elif status in RETRYABLE_STATUSES:
                    self.breaker.on_failure(f"HTTP {status}")
                else:
                    self.breaker.on_success()

                should_retry = status in RETRYABLE_STATUSES and attempt < attempts
                metrics.upstream_requests_total.labels(self.service_name, self.name, "retry" if should_retry else "success").inc()
                metrics.upstream_duration_seconds.labels(self.service_name, self.name).observe(duration)
                logger.info(
                    "upstream call",
                    extra={
                        "target": self.name,
                        "method": method,
                        "path": path,
                        "status": status,
                        "attempt": attempt,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
                if not should_retry:
                    if raise_for_status and ((expected_statuses is None and status >= 400) or (expected_statuses is not None and status not in expected_statuses)):
                        raise self._error_from(response)
                    return response
                # A retryable status falls through to the shared retry handling below.
                outcome = f"http_{status}"

            # Reached only for transport/timeout failures or a retryable status.
            metrics.upstream_requests_total.labels(self.service_name, self.name, outcome).inc()
            if attempt < attempts and safe:
                delay = self.retry.delay_for(attempt)
                logger.warning(
                    "upstream call failed, retrying",
                    extra={"target": self.name, "method": method, "path": path, "attempt": attempt, "max_attempts": attempts, "retry_in_seconds": round(delay, 3), "reason": outcome},
                )
                self.retries_total += 1
                await asyncio.sleep(delay)
                continue
            self.failures_total += 1
            logger.error(
                "upstream call failed",
                extra={"target": self.name, "method": method, "path": path, "attempt": attempt, "reason": outcome},
            )
            assert last_error is not None
            raise last_error

        raise last_error or UpstreamError(f"{self.name} call failed")  # pragma: no cover

    async def call(self, method: str, path: str, **kwargs: Any) -> CallResult:
        started = time.perf_counter()
        response = await self.request(method, path, **kwargs)
        return CallResult(response=response, attempts=1, duration_ms=(time.perf_counter() - started) * 1000)

    async def get_json(self, path: str, *, params: Mapping[str, Any] | None = None, expected: Sequence[int] = (200,)) -> Any:
        response = await self.request("GET", path, params=params, expected=expected)
        return response.json() if response.content else None

    async def post_json(self, path: str, *, json: Any = None, retry_safe: bool = False, expected: Sequence[int] = (200, 201, 202)) -> Any:
        response = await self.request("POST", path, json=json, expected=expected, retry_safe=retry_safe)
        return response.json() if response.content else None

    # ------------------------------------------------------------------ helpers

    def _error_from(self, response: httpx.Response) -> AppError:
        status = response.status_code
        code: str | None = None
        message: str | None = None
        details: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                raw_details = error.get("details")
                if isinstance(raw_details, dict):
                    details = raw_details
            elif "detail" in body:
                message = str(body["detail"])
        details.setdefault("dependency", self.name)
        details["upstream_status"] = status
        details["upstream_code"] = code

        if status == 404:
            return UpstreamError(
                message or f"{self.name} reported that the resource does not exist",
                code=code or "upstream_not_found",
                status_code=404,
                details=details,
            )
        if status in (401, 403):
            return UpstreamError(
                message or f"{self.name} rejected this service's credentials",
                code=code or "upstream_unauthorized",
                status_code=502,
                details=details,
            )
        if status == 409 or status == 422:
            return UpstreamError(message or f"{self.name} rejected the request", code=code or "upstream_rejected", status_code=409, details=details)
        if status == 429:
            return DependencyUnavailableError(
                f"{self.name} is rate limiting this service", code="upstream_rate_limited", details=details
            )
        return UpstreamError(
            message or f"{self.name} failed with status {status}",
            code=code or "upstream_error",
            details=details,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "target": self.name,
            "base_url": self.base_url,
            "requests_total": self.requests_total,
            "failures_total": self.failures_total,
            "retries_total": self.retries_total,
            "circuit": self.breaker.snapshot(),
        }


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerPolicy",
    "HttpClient",
    "RetryPolicy",
    "TimeoutBudget",
]
