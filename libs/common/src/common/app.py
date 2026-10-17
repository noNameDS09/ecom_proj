"""Service bootstrap.

``create_app`` gives every service the same operational shell:

* JSON/console logging with request + correlation ids
* uniform error envelope and 500 handling
* access logs and Prometheus metrics
* ``/live``, ``/ready`` (dependency aware), ``/version``, ``/metrics``
* lifecycle management for the database, migrations, Kafka clients, upstream
  HTTP clients and background tasks (outbox relay, consumers)

A service only wires its own domain code in ``bootstrap``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from starlette.middleware.cors import CORSMiddleware

from common import health, middleware, migrations
from common.config import Settings
from common.db import Database
from common.errors import AppError, install_error_handlers
from common.events import EventConsumer, EventPublisher, NullEventPublisher
from common.httpclient import HttpClient
from common.logging import configure_logging, get_logger

logger = get_logger("common.app")

BootstrapHook = Callable[[FastAPI, "Runtime"], Awaitable[None]]


@dataclass
class Runtime:
    """Per-process handles to everything the service needs at runtime."""

    name: str
    version: str
    settings: Settings
    db: Database
    clients: dict[str, HttpClient] = field(default_factory=dict)
    publisher: EventPublisher | None = None
    consumers: list[EventConsumer] = field(default_factory=list)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    ready: bool = False

    # ------------------------------------------------------------------ wiring

    async def add_client(self, client: HttpClient, *, name: str | None = None) -> HttpClient:
        key = name or client.name
        await client.start()
        self.clients[key] = client
        return client

    async def set_publisher(self, publisher: EventPublisher) -> EventPublisher:
        await publisher.start()
        self.publisher = publisher
        return publisher

    async def start_consumer(self, consumer: EventConsumer, *, name: str) -> EventConsumer:
        await consumer.start()
        self.consumers.append(consumer)
        self.spawn(name, consumer.run)
        return consumer

    def spawn(self, name: str, factory: Callable[[], Awaitable[None]], *, restart_backoff: float = 1.0) -> asyncio.Task[None]:
        """Run a long-lived coroutine with supervision (restart on crash)."""
        if name in self.tasks and not self.tasks[name].done():
            raise RuntimeError(f"background task {name!r} is already running")

        async def _supervised() -> None:
            backoff = restart_backoff
            while True:
                try:
                    await factory()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "background task crashed, restarting",
                        extra={"task": name, "error": f"{type(exc).__name__}: {exc}", "retry_in_seconds": backoff},
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                else:
                    logger.info("background task finished", extra={"task": name})
                    return

        task = asyncio.create_task(_supervised(), name=name)
        self.tasks[name] = task
        return task

    # ------------------------------------------------------------------ lifecycle

    async def ready_checks(self) -> dict[str, Any]:
        checks: dict[str, Any] = {"database": await self.db.ping()}
        if self.publisher is not None:
            await self.publisher.healthy()
        return checks

    async def shutdown(self) -> None:
        self.ready = False
        for name, task in list(self.tasks.items()):
            logger.info("stopping background task", extra={"task": name})
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.tasks.clear()

        for consumer in self.consumers:
            with contextlib.suppress(Exception):
                await consumer.stop()
        self.consumers.clear()

        if self.publisher is not None:
            with contextlib.suppress(Exception):
                await self.publisher.close()
            self.publisher = None

        for client in self.clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        self.clients.clear()

        with contextlib.suppress(Exception):
            await self.db.dispose()
        logger.info("runtime shut down", extra={"service": self.name})


def create_app(
    *,
    settings: Settings,
    name: str,
    version: str | None = None,
    title: str | None = None,
    description: str | None = None,
    routers: Sequence[APIRouter] = (),
    bootstrap: BootstrapHook | None = None,
    cors_origins: Iterable[str] = (),
) -> FastAPI:
    version = version or settings.service_version
    configure_logging(
        service=name,
        version=version,
        environment=settings.environment,
        level=settings.log_level,
        fmt=settings.log_format,
    )
    service_logger = get_logger(name)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.service_name = name  # keep logs/DB application_name truthful
        app.state.settings = settings
        runtime = Runtime(name=name, version=version, settings=settings, db=Database(settings))
        app.state.rt = runtime
        startup_started = asyncio.get_running_loop().time()

        service_logger.info(
            "starting service",
            extra={
                "environment": settings.environment,
                "database": _safe_dsn(settings.asyncpg_dsn),
                "kafka_enabled": settings.kafka.enabled,
            },
        )
        await runtime.db.wait_until_ready()

        if settings.run_migrations:
            applied = await migrations.run_migrations(settings.asyncpg_dsn, settings.migrations_dir)
            service_logger.info("migrations complete", extra={"applied": applied} if applied else {})

        health.register_readiness(app, "database", runtime.db.ping)

        if bootstrap is not None:
            await bootstrap(app, runtime)

        runtime.ready = True
        service_logger.info(
            "service ready",
            extra={
                "startup_seconds": round(asyncio.get_running_loop().time() - startup_started, 3),
                "consumers": [consumer.group_id for consumer in runtime.consumers],
            },
        )
        try:
            yield
        finally:
            service_logger.info("shutting down service", extra={"grace_seconds": settings.shutdown_grace_seconds})
            await runtime.shutdown()

    app = FastAPI(
        title=title or f"{name} API",
        description=description,
        version=version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Platform team"},
        license_info={"name": "MIT"},
    )

    install_error_handlers(app)
    middleware.add_middleware(
        app,
        service_name=name,
        access_log=settings.access_log_enabled,
        metrics_enabled=settings.metrics_enabled,
    )
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    health.install_health_routes(
        app, service=name, version=version, environment=settings.environment, timeout_seconds=settings.readiness_timeout_seconds
    )

    if settings.metrics_enabled:
        from common.metrics import render_metrics

        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics() -> Any:
            from fastapi import Response

            body, content_type = render_metrics()
            return Response(content=body, media_type=content_type)

    @app.get("/", include_in_schema=False)
    async def root(request: Request) -> dict[str, Any]:
        return {
            "service": name,
            "version": version,
            "environment": settings.environment,
            "docs": str(request.url_for("swagger_ui_html")),
            "endpoints": ["/live", "/ready", "/version", "/metrics", "/openapi.json"],
        }

    for router in routers:
        app.include_router(router)

    @app.get("/_runtime", include_in_schema=False)
    async def runtime_snapshot() -> dict[str, Any]:
        """Operational snapshot: dependency health + resilience counters."""
        rt: Runtime = app.state.rt
        return {
            "service": name,
            "version": version,
            "ready": rt.ready,
            "background_tasks": {key: ("running" if not task.done() else "stopped") for key, task in rt.tasks.items()},
            "upstreams": {key: client.snapshot() for key, client in rt.clients.items()},
            "consumers": [
                {"group_id": consumer.group_id, "topics": consumer.topics, "stats": consumer.stats} for consumer in rt.consumers
            ],
            "publisher": type(rt.publisher).__name__ if rt.publisher else None,
        }

    return app


def _safe_dsn(dsn: str) -> str:
    """Strip credentials before logging a connection string."""
    if "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


__all__ = ["AppError", "NullEventPublisher", "Runtime", "create_app"]
