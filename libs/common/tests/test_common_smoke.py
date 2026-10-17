"""Smoke tests for the shared runtime library.

These cover the pure, framework-free pieces every service relies on: money
serialisation, error envelopes, JWT issue/verify, password hashing, the TTL
cache and context propagation. Database, HTTP and Kafka behaviour is exercised
by the per-service and cross-service test suites.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from common import context
from common.cache import TTLCache
from common.config import Settings
from common.errors import NotFoundError, UnauthorizedError
from common.schemas import Money, Page, PositiveMoney
from common.security import (
    decode_access_token,
    hash_password,
    issue_access_token,
    validate_password_strength,
    verify_password,
)


class _Priced(BaseModel):
    amount: Money


class _Line(BaseModel):
    price: PositiveMoney


# --------------------------------------------------------------------------- money


def test_money_serialises_as_string_with_two_decimals() -> None:
    assert _Priced(amount=Decimal("19.9")).model_dump(mode="json") == {"amount": "19.90"}


def test_positive_money_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        _Line(price=Decimal("0.00"))


# --------------------------------------------------------------------------- pagination


def test_page_meta_reports_more_results() -> None:
    page = Page[int].build([1, 2, 3], total=10, limit=3, offset=3)
    assert page.meta.total == 10
    assert page.meta.has_more is True

    last = Page[int].build([7, 8], total=5, limit=3, offset=3)
    assert last.meta.has_more is False


# --------------------------------------------------------------------------- errors


def test_errors_share_the_documented_envelope() -> None:
    error = NotFoundError("no such order", details={"order_id": "o-1"})
    assert error.status_code == 404
    assert error.to_payload() == {
        "error": {"code": "not_found", "message": "no such order", "details": {"order_id": "o-1"}, "request_id": None}
    }


# --------------------------------------------------------------------------- auth primitives


def _settings(secret: str = "smoke-test-secret-0123456789abcdef") -> Settings:
    return Settings(_env_file=None, jwt_secret=secret)


def test_password_hash_roundtrip_and_corrupt_hash() -> None:
    hashed = hash_password("correct-horse-1", rounds=4)
    assert hashed != "correct-horse-1"
    assert verify_password("correct-horse-1", hashed)
    assert not verify_password("wrong-password", hashed)
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_validate_password_strength_reasons() -> None:
    validate_password_strength("correct-horse-1")
    with pytest.raises(ValueError, match="at least 8 characters"):
        validate_password_strength("short")
    with pytest.raises(ValueError, match="at most 72 bytes"):
        validate_password_strength("a" * 73)
    with pytest.raises(ValueError, match="at least one letter"):
        validate_password_strength("12345678")


def test_jwt_roundtrip_carries_the_principal() -> None:
    settings = _settings()
    issued = issue_access_token(subject="u-1", email="ada@example.com", roles=["customer"], settings=settings)

    principal = decode_access_token(issued.token, settings)
    assert principal.user_id == "u-1"
    assert principal.email == "ada@example.com"
    assert principal.has_role("customer")
    assert not principal.is_admin


def test_jwt_from_a_foreign_secret_is_rejected() -> None:
    token = issue_access_token(subject="u-1", email="ada@example.com", roles=[], settings=_settings()).token

    with pytest.raises(UnauthorizedError) as exc:
        decode_access_token(token, _settings("another-foreign-secret-0123456789abcdef"))
    assert exc.value.code == "invalid_token"


def test_expired_jwt_is_reported_as_expired() -> None:
    settings = _settings()
    token = issue_access_token(subject="u-1", email="ada@example.com", roles=[], settings=settings, ttl_seconds=-5).token

    with pytest.raises(UnauthorizedError) as exc:
        decode_access_token(token, settings)
    assert exc.value.code == "token_expired"


def test_asyncpg_dsn_strips_the_sqlalchemy_driver() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://app:app@db:5432/order_db")
    assert settings.asyncpg_dsn == "postgresql://app:app@db:5432/order_db"


# --------------------------------------------------------------------------- cache


async def test_ttl_cache_tracks_hits_misses_and_evictions() -> None:
    cache: TTLCache[str, int] = TTLCache(maxsize=2, ttl=30.0)
    assert await cache.get("a") is None  # miss

    await cache.set("a", 1)
    await cache.set("b", 2)
    assert await cache.get("a") == 1  # moves "a" to most-recently-used

    await cache.set("c", 3)  # evicts "b"
    assert await cache.get("b") is None
    assert cache.stats()["evictions"] == 1

    await cache.invalidate("a")
    assert cache.size == 1


async def test_ttl_cache_expires_entries() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=0.01)
    await cache.set("k", 1)

    await asyncio.sleep(0.02)

    assert await cache.get("k") is None
    assert cache.size == 0


# --------------------------------------------------------------------------- context propagation


def test_bind_context_roundtrip() -> None:
    before = context.snapshot()

    with context.bind_context(request_id="req_test", principal_id="user-1"):
        snapshot = context.snapshot()
        assert snapshot["request_id"] == "req_test"
        assert snapshot["principal_id"] == "user-1"
        assert context.correlation_headers()[context.REQUEST_ID_HEADER] == "req_test"

    assert context.snapshot() == before
