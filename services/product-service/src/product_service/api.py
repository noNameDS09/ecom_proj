"""HTTP API for the product service.

Authorization model
-------------------
* catalogue reads      : public
* catalogue writes     : ``admin`` role
* stock reservations   : internal service token (called by order-service), or admin
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status

from common.auth import Principal, internal_or_admin, require_roles
from common.errors import ConflictError
from common.logging import get_logger
from common.schemas import DeleteResponse, ErrorResponse, Page, PageParams
from product_service.repository import ProductFilters
from product_service.schemas import (
    CategoryFacets,
    ProductCreate,
    ProductRead,
    ProductSummary,
    ProductUpdate,
    ReservationMutationResult,
    ReservationRead,
    ReservationRequest,
    StockLevel,
)
from product_service.service import ProductCatalogue

logger = get_logger("product_service.api")

router = APIRouter(prefix="/products", tags=["products"])
internal_router = APIRouter(prefix="/products", tags=["internal"])

admin_only = require_roles("admin")
internal_caller = internal_or_admin

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}


def get_catalogue(request: Request) -> ProductCatalogue:
    return request.app.state.catalogue


Catalogue = Annotated[ProductCatalogue, Depends(get_catalogue)]


# --------------------------------------------------------------------------- catalogue


@router.post(
    "",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
    responses={409: ERROR_RESPONSES[409], 422: ERROR_RESPONSES[422]},
)
async def create_product(payload: ProductCreate, catalogue: Catalogue, principal: Principal = Depends(admin_only)) -> ProductRead:
    product = await catalogue.create(payload)
    logger.info("product created via API", extra={"product_id": str(product.id), "actor": principal.user_id})
    return product


@router.get("", response_model=Page[ProductRead], summary="List/filter the catalogue")
async def list_products(
    catalogue: Catalogue,
    params: Annotated[PageParams, Depends(PageParams.as_query)],
    q: Annotated[str | None, Query(max_length=120, description="Substring match on name, description or SKU")] = None,
    category: Annotated[str | None, Query(max_length=80)] = None,
    ids: Annotated[str | None, Query(description="Comma separated product ids (max 100)")] = None,
    active: Annotated[bool | None, Query()] = None,
    in_stock: Annotated[bool | None, Query(description="Only products with available stock")] = None,
    min_price: Annotated[Decimal | None, Query(ge=0)] = None,
    max_price: Annotated[Decimal | None, Query(ge=0)] = None,
    sort: Annotated[str, Query(description="name | price | stock | created_at | updated_at, prefix with - for desc")] = "-created_at",
    include_deleted: Annotated[bool, Query(description="Admin only: include soft-deleted products")] = False,
) -> Page[ProductRead]:
    product_ids = _parse_ids(ids)
    filters = ProductFilters(
        q=q,
        category=category,
        ids=product_ids,
        active=active,
        in_stock=in_stock,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
        include_deleted=include_deleted,
    )
    return await catalogue.list(filters, limit=params.limit, offset=params.offset)


@router.get("/categories", response_model=CategoryFacets, summary="Distinct active categories with product counts")
async def list_categories(catalogue: Catalogue) -> CategoryFacets:
    return await catalogue.categories()


@router.get("/{product_id}", response_model=ProductRead, summary="Get a product", responses={404: ERROR_RESPONSES[404]})
async def get_product(product_id: Annotated[uuid.UUID, Path()], catalogue: Catalogue) -> ProductRead:
    return await catalogue.get(product_id)


@router.get("/{product_id}/stock", response_model=StockLevel, summary="Stock levels for a product", responses={404: ERROR_RESPONSES[404]})
async def get_stock(product_id: Annotated[uuid.UUID, Path()], catalogue: Catalogue) -> StockLevel:
    return await catalogue.stock(product_id)


@router.patch(
    "/{product_id}",
    response_model=ProductRead,
    summary="Update a product (PATCH semantics)",
    responses={404: ERROR_RESPONSES[404], 409: ERROR_RESPONSES[409]},
)
async def update_product(
    product_id: Annotated[uuid.UUID, Path()],
    payload: ProductUpdate,
    catalogue: Catalogue,
    principal: Principal = Depends(admin_only),
    if_match: Annotated[int | None, Header(alias="If-Match", description="Expected product version (optimistic locking)")] = None,
) -> ProductRead:
    product = await catalogue.update(product_id, payload, expected_version=if_match)
    logger.info("product updated via API", extra={"product_id": str(product_id), "actor": principal.user_id, "version": product.version})
    return product


@router.delete(
    "/{product_id}",
    response_model=DeleteResponse,
    summary="Soft delete a product",
    responses={404: ERROR_RESPONSES[404], 409: ERROR_RESPONSES[409]},
)
async def delete_product(
    product_id: Annotated[uuid.UUID, Path()],
    catalogue: Catalogue,
    response: Response,
    principal: Principal = Depends(admin_only),
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> DeleteResponse:
    product = await catalogue.delete(product_id, expected_version=if_match)
    response.headers["X-Product-Version"] = str(product.version)
    logger.info("product deleted via API", extra={"product_id": str(product_id), "actor": principal.user_id})
    return DeleteResponse(id=str(product.id))


# --------------------------------------------------------------------------- internal: batching


@internal_router.post(
    "/lookup",
    response_model=list[ProductSummary],
    summary="Batch product lookup (internal)",
    include_in_schema=True,
)
async def lookup_products(
    payload: dict[str, list[uuid.UUID]],
    catalogue: Catalogue,
    caller: str = Depends(internal_caller),
) -> list[ProductSummary]:
    ids = payload.get("ids") or []
    if len(ids) > 100:
        raise ConflictError("At most 100 ids may be looked up at once", code="too_many_ids")
    return await catalogue.get_many(ids)


# --------------------------------------------------------------------------- internal: reservations


@internal_router.post(
    "/reservations",
    response_model=ReservationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Reserve stock for an order (internal, idempotent by order_id)",
    responses={404: ERROR_RESPONSES[404], 409: ERROR_RESPONSES[409], 422: ERROR_RESPONSES[422]},
)
async def create_reservation(
    payload: ReservationRequest,
    catalogue: Catalogue,
    response: Response,
    caller: str = Depends(internal_caller),
) -> ReservationRead:
    reservation, created = await catalogue.reserve(payload)
    if not created:
        # Same order id, same lines: the caller is retrying, hand back the original.
        response.status_code = status.HTTP_200_OK
    response.headers["X-Idempotent-Replay"] = "false" if created else "true"
    logger.info(
        "reservation handled",
        extra={"order_id": str(payload.order_id), "is_new": created, "caller": caller, "items": len(reservation.items)},
    )
    return reservation


@internal_router.get(
    "/reservations/{order_id}",
    response_model=ReservationRead,
    summary="Fetch a reservation (internal)",
    responses={404: ERROR_RESPONSES[404]},
)
async def get_reservation(
    order_id: Annotated[uuid.UUID, Path()], catalogue: Catalogue, caller: str = Depends(internal_caller)
) -> ReservationRead:
    return await catalogue.get_reservation(order_id)


@internal_router.post(
    "/reservations/{order_id}/release",
    response_model=ReservationMutationResult,
    summary="Release reserved stock (internal, compensating action)",
)
async def release_reservation(
    order_id: Annotated[uuid.UUID, Path()], catalogue: Catalogue, caller: str = Depends(internal_caller)
) -> ReservationMutationResult:
    return await catalogue.release(order_id)


@internal_router.post(
    "/reservations/{order_id}/commit",
    response_model=ReservationMutationResult,
    summary="Commit reserved stock after payment (internal)",
    responses={409: ERROR_RESPONSES[409]},
)
async def commit_reservation(
    order_id: Annotated[uuid.UUID, Path()], catalogue: Catalogue, caller: str = Depends(internal_caller)
) -> ReservationMutationResult:
    return await catalogue.commit(order_id)


def _parse_ids(raw: str | None) -> list[uuid.UUID]:
    if not raw:
        return []
    values = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    if len(values) > 100:
        raise ConflictError("At most 100 ids may be filtered at once", code="too_many_ids")
    parsed: list[uuid.UUID] = []
    for value in values:
        try:
            parsed.append(uuid.UUID(value))
        except ValueError as exc:
            raise ConflictError(f"'{value}' is not a valid product id", code="invalid_product_id") from exc
    return parsed
