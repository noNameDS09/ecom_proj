"""Transactional outbox, consumer de-duplication and dead-letter persistence.

These three tables make the event flow reliable:

``outbox_events``     written in the *same transaction* as the business change;
                      published by :class:`OutboxRelay` with at-least-once semantics.
``processed_events``  consumer-side inbox: a redelivered ``event_id`` is a no-op.
``failed_events``     handler failures parked for inspection/replay instead of
                      blocking a Kafka partition.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, Text, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from common import context, metrics
from common.db import Base, Database
from common.events import EVENT_TYPE_HEADER, Event, EventAlreadyProcessed, EventPublisher
from common.logging import get_logger

logger = get_logger("common.outbox")

STATUS_PENDING = "pending"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    # Monotonic id: guarantees the relay publishes in the order events were created.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    partition_key: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_outbox_events_status_next_attempt_at", "status", "next_attempt_at"),
    )


class ProcessedEvent(Base):
    """Consumer inbox: one row per (event, consumer) that was applied."""

    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    consumer: Mapped[str] = mapped_column(String(120), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class FailedEvent(Base):
    """Dead-letter storage for events a consumer could not process."""

    __tablename__ = "failed_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    consumer: Mapped[str] = mapped_column(String(120), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# --------------------------------------------------------------------------- producer side


def build_event(
    *,
    event_type: str,
    data: dict[str, Any],
    topic: str,
    key: str | None = None,
    producer: str,
    headers: Mapping[str, str] | None = None,
) -> Event:
    return Event(
        event_type=event_type,
        data=data,
        topic=topic,
        key=key,
        producer=producer,
        correlation_id=context.get_correlation_id(),
        headers=dict(headers or {}),
    )


def enqueue_event(session: AsyncSession, event: Event) -> OutboxEvent:
    """Append an event to the outbox inside the caller's transaction.

    Nothing is sent to Kafka here - the caller's commit is what makes the event
    visible to the relay, which is exactly what keeps the write and the event
    atomic without a distributed transaction.
    """
    row = OutboxEvent(
        event_id=event.event_id,
        topic=event.topic,
        event_type=event.event_type,
        partition_key=event.key,
        payload=event.to_message(),
        headers=dict(event.headers),
        occurred_at=event.occurred_at,
    )
    session.add(row)
    logger.debug("event queued in outbox", extra={"event_type": event.event_type, "topic": event.topic, "event_id": event.event_id})
    return row


# --------------------------------------------------------------------------- consumer side


async def claim_event(session: AsyncSession, *, event: Event, consumer: str) -> None:
    """Mark the event as consumed; raise :class:`EventAlreadyProcessed` on redelivery.

    Must be called in the same transaction as the handler's business write: either
    both land or neither does, so a crash can never lose the dedup marker while
    keeping the effect.
    """
    result = await session.execute(
        pg_insert(ProcessedEvent)
        .values(event_id=event.event_id, consumer=consumer, event_type=event.event_type)
        .on_conflict_do_nothing(index_elements=["event_id", "consumer"])
        .returning(ProcessedEvent.event_id)
    )
    if result.scalar_one_or_none() is None:
        raise EventAlreadyProcessed(f"{event.event_type} {event.event_id} was already applied by {consumer}")


async def record_failed_event(session: AsyncSession, *, event: Event, consumer: str, error: Exception, attempts: int) -> None:
    session.add(
        FailedEvent(
            event_id=event.event_id,
            consumer=consumer,
            topic=event.topic,
            event_type=event.event_type,
            payload=event.to_message(),
            error=f"{type(error).__name__}: {error}"[:4000],
            attempts=attempts,
        )
    )


def make_dlq_handler(
    *,
    database: Database,
    publisher: EventPublisher,
    service_name: str,
    dlq_topic: str,
) -> Any:
    """Build the ``on_dlq`` callback used by :class:`common.events.EventConsumer`."""

    async def _handler(event: Event, error: Exception, attempts: int) -> None:
        async with database.session() as session:
            await record_failed_event(session, event=event, consumer=service_name, error=error, attempts=attempts)

        dead_letter = Event(
            event_type="event.dead_lettered",
            topic=dlq_topic,
            key=event.key or event.event_id,
            producer=service_name,
            correlation_id=event.correlation_id,
            headers={EVENT_TYPE_HEADER: event.event_type},
            data={
                "original_event": event.to_message(),
                "consumer": service_name,
                "attempts": attempts,
                "error": f"{type(error).__name__}: {error}"[:2000],
            },
        )
        try:
            await publisher.publish(dead_letter)
        except Exception as exc:
            logger.error(
                "could not publish to the dead letter topic",
                extra={"event_id": event.event_id, "dlq_topic": dlq_topic, "error": str(exc)},
            )

    return _handler


# --------------------------------------------------------------------------- relay


@dataclass(slots=True)
class OutboxStats:
    published: int = 0
    failed: int = 0
    backlog: int = 0


class OutboxRelay:
    """Background publisher that drains ``outbox_events`` to Kafka.

    * claim (short transaction, ``FOR UPDATE SKIP LOCKED``) -> publish -> mark
    * failed publishes go back to ``pending`` with exponential backoff
    * after ``max_attempts`` the row is parked as ``failed`` and alerted on
    * claims abandoned by a crashed pod are reclaimed after ``stale_claim_seconds``
      (a re-publish is harmless: consumers de-duplicate by ``event_id``)
    """

    def __init__(
        self,
        *,
        database: Database,
        publisher: EventPublisher,
        service_name: str,
        poll_interval: float = 0.5,
        batch_size: int = 50,
        max_attempts: int = 8,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        stale_claim_seconds: float = 60.0,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.service_name = service_name
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.stale_claim_seconds = stale_claim_seconds
        self._stop = asyncio.Event()
        self.stats = OutboxStats()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("outbox relay started", extra={"poll_interval": self.poll_interval, "batch_size": self.batch_size})
        while not self._stop.is_set():
            try:
                processed = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("outbox drain failed", extra={"error": f"{type(exc).__name__}: {exc}"})
                processed = 0
            if processed:
                continue  # keep draining while there is work
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
        logger.info("outbox relay stopped", extra={"published": self.stats.published, "failed": self.stats.failed})

    async def drain_once(self) -> int:
        """Claim and publish one batch. Returns the number of events confirmed."""
        claimed = await self._claim()
        await self._refresh_backlog()
        if not claimed:
            return 0

        delivered = 0
        for row in claimed:
            event = Event(
                event_type=row.event_type,
                data=(row.payload or {}).get("data") or {},
                topic=row.topic,
                key=row.partition_key,
                event_id=row.event_id,
                occurred_at=row.occurred_at,
                producer=row.payload.get("producer", self.service_name),
                correlation_id=row.payload.get("correlation_id"),
                headers={str(k): str(v) for k, v in (row.headers or {}).items()},
            )
            try:
                await self.publisher.publish(event)
            except Exception as exc:
                await self._mark_failed(row.id, exc)
            else:
                await self._mark_published(row.id)
                delivered += 1
                self.stats.published += 1
        return delivered

    async def _claim(self) -> list[OutboxEvent]:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.stale_claim_seconds)
        async with self.database.session() as session:
            claimed = (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        or_(
                            and_(OutboxEvent.status == STATUS_PENDING, OutboxEvent.next_attempt_at <= func.now()),
                            and_(OutboxEvent.status == STATUS_PUBLISHING, OutboxEvent.claimed_at < stale_before),
                        )
                    )
                    .order_by(OutboxEvent.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for row in claimed:
                if row.status == STATUS_PUBLISHING:
                    logger.warning(
                        "reclaiming stale outbox claim",
                        extra={"event_id": row.event_id, "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None},
                    )
                row.status = STATUS_PUBLISHING
                row.claimed_at = datetime.now(UTC)
            return list(claimed)

    async def _mark_published(self, row_id: int) -> None:
        async with self.database.session() as session:
            await session.execute(
                text(
                    "UPDATE outbox_events SET status = :published, published_at = now(), last_error = NULL "
                    "WHERE id = :id"
                ),
                {"published": STATUS_PUBLISHED, "id": row_id},
            )

    async def _mark_failed(self, row_id: int, error: Exception) -> None:
        async with self.database.session() as session:
            row = (
                await session.execute(select(OutboxEvent).where(OutboxEvent.id == row_id).with_for_update())
            ).scalar_one()
            self.stats.failed += 1
            metrics.outbox_publish_failures_total.labels(self.service_name).inc()
            row.last_error = f"{type(error).__name__}: {error}"[:2000]
            if row.attempts >= self.max_attempts:
                row.status = STATUS_FAILED
                logger.error(
                    "outbox event parked after exhausting attempts",
                    extra={"event_id": row.event_id, "event_type": row.event_type, "attempts": row.attempts, "error": row.last_error},
                )
            else:
                backoff = min(self.backoff_base * (2 ** max(0, row.attempts - 1)), self.backoff_max)
                delay = backoff * (0.5 + random.random() / 2)  # jitter avoids retry storms
                row.status = STATUS_PENDING
                row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                logger.warning(
                    "outbox publish failed, will retry",
                    extra={
                        "event_id": row.event_id,
                        "event_type": row.event_type,
                        "attempts": row.attempts,
                        "retry_in_seconds": round(delay, 2),
                        "error": row.last_error,
                    },
                )

    async def _refresh_backlog(self) -> None:
        async with self.database.read_session() as session:
            pending = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_events WHERE status IN (:pending, :publishing)"),
                    {"pending": STATUS_PENDING, "publishing": STATUS_PUBLISHING},
                )
            ).scalar_one()
        self.stats.backlog = int(pending)
        metrics.outbox_backlog.labels(self.service_name).set(int(pending))

    async def backlog(self) -> int:
        async with self.database.read_session() as session:
            return int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM outbox_events WHERE status IN (:pending, :publishing)"),
                        {"pending": STATUS_PENDING, "publishing": STATUS_PUBLISHING},
                    )
                ).scalar_one()
            )


__all__ = [
    "FailedEvent",
    "OutboxEvent",
    "OutboxRelay",
    "ProcessedEvent",
    "build_event",
    "claim_event",
    "enqueue_event",
    "make_dlq_handler",
    "record_failed_event",
]
