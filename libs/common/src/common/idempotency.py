"""Idempotent write endpoints (``Idempotency-Key`` header).

Used by order creation and payment capture: a client that retries a timed out
POST gets the original response instead of a duplicate order/charge.

State machine per (key, endpoint, principal):

    claim:  INSERT ... ON CONFLICT DO NOTHING
              -> inserted        : we own the request, proceed
              -> already done    : replay the stored response
              -> in progress     : 409 (client retries later) unless the claim is
                                   stale (crashed caller) in which case we take over
    done:   store status code + body, replayable for ``ttl_seconds``
    release: delete the claim when the request failed, so the client can retry
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import DateTime, Integer, String, delete, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base, Database
from common.errors import ValidationError
from common.logging import get_logger

logger = get_logger("common.idempotency")

HeaderValue = Literal["proceed", "replay", "in_flight", "key_reuse"]


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(200), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="anonymous")
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(slots=True)
class IdempotencyClaim:
    outcome: HeaderValue
    key: str
    request_hash: str
    principal_id: str
    response_status: int | None = None
    response_body: dict[str, Any] | None = None
    resource_id: str | None = None

    @property
    def should_proceed(self) -> bool:
        return self.outcome == "proceed"


class IdempotencyGuard:
    def __init__(
        self,
        database: Database,
        *,
        endpoint: str,
        ttl_seconds: int = 24 * 3600,
        claim_timeout_seconds: int = 60,
    ) -> None:
        self.database = database
        self.endpoint = endpoint
        self.ttl_seconds = ttl_seconds
        self.claim_timeout_seconds = claim_timeout_seconds

    @staticmethod
    def fingerprint(payload: Any) -> str:
        """Stable hash of the request body so a key cannot be reused with new data."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_key(key: str | None) -> str:
        if key is None or not key.strip():
            raise ValidationError(
                "The Idempotency-Key header is required for this endpoint",
                code="idempotency_key_required",
                details={"header": "Idempotency-Key"},
            )
        key = key.strip()
        if len(key) > 200:
            raise ValidationError("Idempotency-Key must be at most 200 characters", code="idempotency_key_too_long")
        return key

    async def claim(self, *, key: str, request_hash: str, principal_id: str | None = None) -> IdempotencyClaim:
        principal = principal_id or "anonymous"
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        async with self.database.session() as session:
            inserted = (
                await session.execute(
                    pg_insert(IdempotencyRecord)
                    .values(
                        key=key,
                        endpoint=self.endpoint,
                        principal_id=principal,
                        request_hash=request_hash,
                        status="in_progress",
                        claimed_at=now,
                        expires_at=expires_at,
                    )
                    .on_conflict_do_nothing(index_elements=["key", "endpoint", "principal_id"])
                    .returning(IdempotencyRecord.key)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                return IdempotencyClaim(outcome="proceed", key=key, request_hash=request_hash, principal_id=principal)

            record = (
                await session.execute(
                    select(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.key == key,
                        IdempotencyRecord.endpoint == self.endpoint,
                        IdempotencyRecord.principal_id == principal,
                    )
                    .with_for_update()
                )
            ).scalar_one()

            if record.request_hash != request_hash:
                logger.warning("idempotency key reused with a different payload", extra={"key": key, "endpoint": self.endpoint})
                return IdempotencyClaim(outcome="key_reuse", key=key, request_hash=request_hash, principal_id=principal)

            if record.status == "completed":
                logger.info("replaying stored response for idempotent request", extra={"key": key, "endpoint": self.endpoint})
                return IdempotencyClaim(
                    outcome="replay",
                    key=key,
                    request_hash=request_hash,
                    principal_id=principal,
                    response_status=record.response_status,
                    response_body=record.response_body,
                    resource_id=record.resource_id,
                )

            stale = record.claimed_at < now - timedelta(seconds=self.claim_timeout_seconds)
            if stale:
                logger.warning(
                    "taking over a stale idempotency claim",
                    extra={"key": key, "claimed_at": record.claimed_at.isoformat(), "claim_timeout_seconds": self.claim_timeout_seconds},
                )
                record.status = "in_progress"
                record.claimed_at = now
                record.expires_at = expires_at
                return IdempotencyClaim(outcome="proceed", key=key, request_hash=request_hash, principal_id=principal)

            return IdempotencyClaim(outcome="in_flight", key=key, request_hash=request_hash, principal_id=principal)

    async def complete(
        self,
        claim: IdempotencyClaim,
        *,
        status_code: int,
        body: dict[str, Any] | None,
        resource_id: str | None = None,
    ) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.key == claim.key,
                    IdempotencyRecord.endpoint == self.endpoint,
                    IdempotencyRecord.principal_id == claim.principal_id,
                )
                .values(
                    status="completed",
                    response_status=status_code,
                    response_body=body,
                    resource_id=resource_id,
                    completed_at=datetime.now(UTC),
                )
            )

    async def release(self, claim: IdempotencyClaim) -> None:
        """Drop the in-progress claim after a failure so the client may retry."""
        async with self.database.session() as session:
            await session.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.key == claim.key,
                    IdempotencyRecord.endpoint == self.endpoint,
                    IdempotencyRecord.principal_id == claim.principal_id,
                    IdempotencyRecord.status == "in_progress",
                )
            )

    async def purge_expired(self, *, limit: int = 1000) -> int:
        """Delete expired keys. ``ctid`` targets exact rows regardless of the composite key."""
        async with self.database.session() as session:
            result = await session.execute(
                text(
                    "DELETE FROM idempotency_keys WHERE ctid IN ("
                    "  SELECT ctid FROM idempotency_keys WHERE expires_at < now() LIMIT :limit"
                    ")"
                ),
                {"limit": limit},
            )
            return result.rowcount or 0
