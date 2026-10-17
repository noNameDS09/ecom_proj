"""Product service entrypoint.

Run with ``uvicorn product_service.main:app`` (Docker) or build the ASGI app
explicitly in tests/scripts via :func:`build_app`.
"""

from __future__ import annotations

from fastapi import FastAPI

from common.app import Runtime, create_app
from common.logging import get_logger
from common.migrations import resolve_migrations_dir
from product_service import __version__
from product_service.api import internal_router, router
from product_service.service import ProductCatalogue
from product_service.settings import ProductServiceSettings

logger = get_logger("product_service")


def build_app(settings: ProductServiceSettings | None = None) -> FastAPI:
    settings = settings or ProductServiceSettings()
    settings.migrations_dir = resolve_migrations_dir(settings.migrations_dir, package_file=__file__)

    async def bootstrap(app: FastAPI, runtime: Runtime) -> None:
        app.state.catalogue = ProductCatalogue(runtime.db, settings)

        @app.get("/_catalogue/cache", include_in_schema=False)
        async def cache_stats() -> dict[str, object]:
            return app.state.catalogue.cache_stats()

        logger.info(
            "product service bootstrapped",
            extra={
                "cache_ttl_seconds": settings.cache_ttl_seconds,
                "cache_max_entries": settings.cache_max_entries,
                "migrations_dir": settings.migrations_dir,
            },
        )

    return create_app(
        settings=settings,
        name="product-service",
        version=__version__,
        title="Product Service API",
        description=(
            "Product catalogue with stock reservations.\n\n"
            "* CRUD over products, soft deletes, optimistic locking via `If-Match`.\n"
            "* Reservation ledger with two-phase commit (reserve -> release | commit) so a paid order never oversells.\n"
            "* Reservation endpoints are idempotent by `order_id` and authenticated with an internal service token."
        ),
        routers=(internal_router, router),
        bootstrap=bootstrap,
    )


app = build_app()
