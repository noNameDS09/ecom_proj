"""Shared test infrastructure.

Two building blocks are used across the suite:

* :func:`create_test_database` - a throwaway Postgres database per test, with the
  service migrations applied, so tests never share state.
* :func:`start_service` - runs a real ASGI app with uvicorn in a background
  thread, so cross-service tests exercise real HTTP, real connection pools and
  real timeouts instead of in-process shortcuts.

The Kafka broker is optional: set ``TEST_KAFKA_BOOTSTRAP`` (or run
``docker compose up kafka``) and the integration tests that need it will run,
otherwise they are skipped.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg
import uvicorn
from fastapi import FastAPI

DEFAULT_ADMIN_DSN = os.environ.get("TEST_ADMIN_DSN", "postgresql://app:app@127.0.0.1:5432/postgres")
TEST_JWT_SECRET = "test-secret-not-for-production-0123456789abcdef"
TEST_INTERNAL_TOKEN = "test-internal-token"


def kafka_bootstrap() -> str | None:
    return os.environ.get("TEST_KAFKA_BOOTSTRAP") or None


# --------------------------------------------------------------------------- database helpers


def _dsn_parts(dsn: str) -> dict[str, Any]:
    parts = urlsplit(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parts.hostname or "localhost",
        "port": parts.port or 5432,
        "user": parts.username or "postgres",
        "password": parts.password or "",
        "dbname": (parts.path or "/postgres").lstrip("/") or "postgres",
    }


def sqlalchemy_dsn(admin_dsn: str, database: str) -> str:
    parts = _dsn_parts(admin_dsn)
    credentials = f"{parts['user']}:{parts['password']}" if parts["password"] else parts["user"]
    return f"postgresql+asyncpg://{credentials}@{parts['host']}:{parts['port']}/{database}"


def create_test_database(prefix: str = "test", *, admin_dsn: str | None = None) -> tuple[str, str]:
    """Create a fresh database. Returns ``(database_name, sqlalchemy_dsn)``."""
    admin = admin_dsn or DEFAULT_ADMIN_DSN
    name = f"{prefix}_{uuid.uuid4().hex[:12]}".replace("-", "_")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return name, sqlalchemy_dsn(admin, name)


def drop_test_database(name: str, *, admin_dsn: str | None = None) -> None:
    admin = admin_dsn or DEFAULT_ADMIN_DSN
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def apply_migrations(database_url: str, migrations_dir: str) -> list[str]:
    """Run the SQL migration runner against a freshly created database."""
    from common.migrations import run_migrations

    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://")
    return asyncio.run(run_migrations(dsn, migrations_dir))


def admin_dsn_reachable(admin_dsn: str | None = None) -> bool:
    try:
        with psycopg.connect(admin_dsn or DEFAULT_ADMIN_DSN, connect_timeout=3):
            return True
    except Exception:
        return False


# --------------------------------------------------------------------------- live services


@dataclass
class LiveService:
    """A service running in its own thread with its own event loop."""

    name: str
    url: str
    port: int
    database: str
    settings: Any
    server: uvicorn.Server
    thread: threading.Thread
    logs: list[str] = field(default_factory=list)

    def client(self, **kwargs: Any) -> ServiceClient:
        return ServiceClient(self.url, **kwargs)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def stopped(self) -> bool:
        return not self.thread.is_alive()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_service(
    *,
    app: FastAPI,
    name: str,
    database: str,
    settings: Any,
    port: int | None = None,
    startup_timeout: float = 45.0,
    log_level: str = "warning",
) -> LiveService:
    port = port or free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=10,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"{name}-server", daemon=True)
    thread.start()
    service = LiveService(
        name=name, url=f"http://127.0.0.1:{port}", port=port, database=database, settings=settings, server=server, thread=thread
    )
    _await_http(f"{service.url}/ready", timeout=startup_timeout, what=f"{name} readiness")
    return service


def _await_http(url: str, *, timeout: float, what: str, expected: int | None = 200) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last_error: str = "timed out"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if expected is None or response.status_code == expected:
                return response
            last_error = f"status {response.status_code}: {response.text[:500]}"
        time.sleep(0.25)
    raise AssertionError(f"{what} did not become available within {timeout}s ({last_error})")


def wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0, interval: float = 0.1, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"{what} was not satisfied within {timeout}s")


# --------------------------------------------------------------------------- http client


class ServiceClient:
    """Thin httpx wrapper with helpers for bearer/service-token calls."""

    def __init__(self, base_url: str, *, token: str | None = None, internal_token: str | None = None, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.internal_token = internal_token
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # -- lifecycle
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ServiceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth helpers
    def as_user(self, token: str) -> ServiceClient:
        self.token = token
        return self

    def with_internal_token(self, token: str = TEST_INTERNAL_TOKEN) -> ServiceClient:
        self.internal_token = token
        return self

    def headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
            headers["X-Source-Service"] = "test-harness"
        if extra:
            headers.update(extra)
        return headers

    # -- verbs
    def request(self, method: str, path: str, *, headers: Mapping[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        merged = self.headers(headers)
        merged.setdefault("X-Correlation-ID", f"cor_test_{uuid.uuid4().hex[:10]}")
        return self._client.request(method, path, headers=merged, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    # -- assertions
    def ok(self, response: httpx.Response, expected: int | tuple[int, ...] = (200, 201)) -> Any:
        expected_codes = (expected,) if isinstance(expected, int) else expected
        assert response.status_code in expected_codes, f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text[:800]}"
        return response.json() if response.content else None

    def error(self, response: httpx.Response, *, status: int, code: str) -> dict[str, Any]:
        assert response.status_code == status, f"expected {status}, got {response.status_code}: {response.text[:800]}"
        payload = response.json()
        assert payload["error"]["code"] == code, f"expected error code {code!r}, got {payload['error']}"
        assert payload["error"]["request_id"], "error envelope must carry the request id"
        return payload


# --------------------------------------------------------------------------- auth helpers


def issue_token(*, user_id: str | None = None, email: str = "tester@example.com", roles: tuple[str, ...] = ("customer",), secret: str = TEST_JWT_SECRET) -> str:
    from common.config import Settings
    from common.security import issue_access_token

    settings = Settings(jwt_secret=secret)
    issued = issue_access_token(subject=user_id or uuid.uuid4().hex, email=email, roles=list(roles), settings=settings)
    return issued.token


def admin_token(secret: str = TEST_JWT_SECRET) -> str:
    return issue_token(email="admin@example.com", roles=("admin",), secret=secret)


def base_env(**overrides: str) -> dict[str, str]:
    """Environment for a test instance of a service."""
    env = {
        "ENVIRONMENT": "test",
        "LOG_FORMAT": "console",
        "LOG_LEVEL": "WARNING",
        "JWT_SECRET": TEST_JWT_SECRET,
        "INTERNAL_SERVICE_TOKEN": TEST_INTERNAL_TOKEN,
        "ACCESS_LOG_ENABLED": "false",
        "KAFKA_ENABLED": "false",
        "DB_CONNECT_ATTEMPTS": "5",
        "DB_CONNECT_RETRY_DELAY_SECONDS": "0.2",
    }
    env.update(overrides)
    return env


def apply_env(env: Mapping[str, str]) -> None:
    os.environ.update(env)


__all__ = [
    "DEFAULT_ADMIN_DSN",
    "TEST_INTERNAL_TOKEN",
    "TEST_JWT_SECRET",
    "LiveService",
    "ServiceClient",
    "admin_dsn_reachable",
    "admin_token",
    "apply_env",
    "apply_migrations",
    "base_env",
    "create_test_database",
    "drop_test_database",
    "free_port",
    "issue_token",
    "kafka_bootstrap",
    "start_service",
    "wait_until",
]
