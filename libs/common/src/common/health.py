"""Health endpoints: liveness, readiness (dependency aware) and /version.

``/live``   - is the process running? Never touches dependencies.
``/ready``  - can this instance serve traffic? Checks every registered dependency.
``/health`` - alias of ``/ready`` kept for humans/load balancers.
``/version``- build/service metadata.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from common import metrics
from common.logging import get_logger

logger = get_logger("common.health")

ReadinessCheck = Callable[[], Awaitable[Any]]
_READINESS: dict[str, ReadinessCheck] = {}
_STARTED_AT = time.time()


def register_readiness(app: FastAPI, name: str, check: ReadinessCheck) -> None:
    """Register an async dependency probe used by ``/ready``."""
    registry: dict[str, ReadinessCheck] = app.state.readiness
    registry[name] = check


def _readiness(app: FastAPI) -> dict[str, ReadinessCheck]:
    if not hasattr(app.state, "readiness"):
        app.state.readiness = {}
    return app.state.readiness


def install_health_routes(
    app: FastAPI,
    *,
    service: str,
    version: str,
    environment: str,
    timeout_seconds: float = 3.0,
) -> None:
    _readiness(app)

    @app.get("/live", tags=["health"], summary="Liveness probe")
    async def live() -> dict[str, Any]:
        return {"status": "alive", "service": service, "uptime_seconds": round(time.time() - _STARTED_AT, 3)}

    @app.get("/version", tags=["health"], summary="Service metadata")
    async def version_info() -> dict[str, Any]:
        return {
            "service": service,
            "version": version,
            "environment": environment,
            "uptime_seconds": round(time.time() - _STARTED_AT, 3),
        }

    @app.get("/ready", tags=["health"], summary="Readiness probe")
    async def ready(response: Response) -> JSONResponse:
        registry = _readiness(app)
        checks = {name: asyncio.create_task(asyncio.wait_for(check(), timeout=timeout_seconds)) for name, check in registry.items()}
        results: dict[str, Any] = {}
        healthy = True
        for name, task in checks.items():
            try:
                await task
                results[name] = {"status": "ok"}
                metrics.readiness_status.labels(service, name).set(1)
            except TimeoutError:
                healthy = False
                results[name] = {"status": "error", "error": f"timed out after {timeout_seconds}s"}
                metrics.readiness_status.labels(service, name).set(0)
            except Exception as exc:
                healthy = False
                results[name] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                metrics.readiness_status.labels(service, name).set(0)
        payload = {"status": "ready" if healthy else "degraded", "service": service, "checks": results}
        if not healthy:
            logger.warning("readiness probe failed", extra={"checks": results})
        return JSONResponse(status_code=200 if healthy else 503, content=payload)

    @app.get("/health", tags=["health"], summary="Readiness alias", include_in_schema=False)
    async def health() -> JSONResponse:
        return await ready(Response())
