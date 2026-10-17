"""Typed application errors and the uniform JSON error envelope.

Every service answers with this shape so clients (and other services) can react
to a machine readable ``code`` instead of parsing prose:

    {
      "error": {
        "code": "product_not_found",
        "message": "Product 8f1c... does not exist",
        "details": {"product_id": "8f1c..."},
        "request_id": "req_..."
      }
    }

The envelope is used for *every* failure path: application errors, request
validation errors, unhandled exceptions and auth failures raised by libraries.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from common import context
from common.logging import get_logger

logger = get_logger("common.errors")


class AppError(Exception):
    """Base class for expected, mapped-to-HTTP errors."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "Unexpected error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] = dict(details or {})
        self.headers: dict[str, str] = dict(headers or {})
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": context.get_request_id(),
            }
        }


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "Resource already exists or is in an invalid state"


class InvalidStateTransitionError(ConflictError):
    code = "invalid_state_transition"
    message = "The resource is not in a state that allows this operation"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"
    message = "Request payload is invalid"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Authentication required"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    message = "Insufficient permissions"


class UpstreamError(AppError):
    """A dependency answered with an error we cannot recover from."""

    status_code = 502
    code = "upstream_error"
    message = "Upstream dependency failed"


class UpstreamTimeoutError(UpstreamError):
    status_code = 504
    code = "upstream_timeout"
    message = "Upstream dependency timed out"


class DependencyUnavailableError(AppError):
    status_code = 503
    code = "dependency_unavailable"
    message = "A required dependency is unavailable"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests"


class InternalError(AppError):
    status_code = 500
    code = "internal_error"
    message = "Internal server error"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("request failed", extra={"error_code": exc.code, "detail": exc.message, "details": exc.details}, exc_info=exc)
        else:
            logger.info("request rejected", extra={"error_code": exc.code, "detail": exc.message, "details": exc.details})
        headers = {"X-Error-Code": exc.code, **exc.headers}
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload(), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(part) for part in err.get("loc", ())[1:]) or "(body)", "message": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        error = ValidationError("Request payload is invalid", details={"errors": details})
        return JSONResponse(status_code=error.status_code, content=error.to_payload())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 409: "conflict"}.get(
            exc.status_code, "http_error"
        )
        error = AppError(str(exc.detail), code=code, status_code=exc.status_code)
        return JSONResponse(status_code=exc.status_code, content=error.to_payload())

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception", extra={"error_type": type(exc).__name__})
        error = InternalError()
        return JSONResponse(status_code=error.status_code, content=error.to_payload())
