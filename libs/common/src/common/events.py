"""Domain event envelope, publisher and consumer helpers (Kafka).

Reliability model
-----------------
Producers never publish inline with a request. They append the event to the
``outbox_events`` table **in the same transaction** as the business write; a
background relay (see :mod:`common.outbox`) then publishes it to Kafka and marks
it done. That gives at-least-once delivery without distributed transactions.

Consumers are idempotent: each handler claims the ``event_id`` in
``processed_events`` inside the same transaction as its own write, so a
redelivered message is a no-op. Failed messages are retried a few times and then
parked in the dead-letter topic/table instead of blocking the partition.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import KafkaError

from common import context, metrics
from common.config import KafkaSettings
from common.errors import DependencyUnavailableError
from common.logging import get_logger

logger = get_logger("common.events")

EVENT_ID_HEADER = "event_id"
EVENT_TYPE_HEADER = "event_type"
CORRELATION_ID_HEADER = "correlation_id"


class EventAlreadyProcessed(Exception):
    """Raised by handlers when the event was already applied (dedup hit).

    The consumer treats it as a success: the message is committed and no retry
    or dead-lettering happens.
    """


# Topics used across the platform.
TOPIC_ORDER_EVENTS = "order.events"
TOPIC_PAYMENT_EVENTS = "payment.events"
TOPIC_DLQ = "ecommerce.dlq"


@dataclass(slots=True)
class Event:
    """The wire format for everything that travels over Kafka."""

    event_type: str
    data: dict[str, Any]
    topic: str
    key: str | None = None
    event_id: str = field(default_factory=context.new_event_id)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    producer: str = "unknown"
    correlation_id: str | None = None
    schema_version: int = 1
    headers: dict[str, str] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at.isoformat(),
            "producer": self.producer,
            "correlation_id": self.correlation_id,
            "data": self.data,
        }

    def to_wire(self) -> bytes:
        return json.dumps(self.to_message(), default=str, separators=(",", ":")).encode("utf-8")

    def message_key(self) -> bytes | None:
        return self.key.encode("utf-8") if self.key else None

    def kafka_headers(self) -> list[tuple[str, bytes]]:
        merged = {
            EVENT_ID_HEADER: self.event_id,
            EVENT_TYPE_HEADER: self.event_type,
            "producer": self.producer,
            **({CORRELATION_ID_HEADER: self.correlation_id} if self.correlation_id else {}),
            **self.headers,
        }
        return [(key, value.encode("utf-8")) for key, value in merged.items()]

    @classmethod
    def from_message(cls, *, topic: str, raw: bytes | str, headers: Mapping[str, str] | None = None, key: str | None = None) -> Event:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"event payload is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or "event_type" not in payload:
            raise ValueError("event payload is missing required fields")
        occurred_raw = payload.get("occurred_at")
        occurred_at = datetime.fromisoformat(occurred_raw) if isinstance(occurred_raw, str) else datetime.now(UTC)
        headers = dict(headers or {})
        return cls(
            event_type=str(payload["event_type"]),
            data=payload.get("data") or {},
            topic=topic,
            key=key,
            event_id=str(payload.get("event_id") or headers.get(EVENT_ID_HEADER) or context.new_event_id()),
            occurred_at=occurred_at,
            producer=str(payload.get("producer", "unknown")),
            correlation_id=payload.get("correlation_id") or headers.get(CORRELATION_ID_HEADER),
            schema_version=int(payload.get("schema_version", 1)),
            headers=headers,
        )

    def to_row(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at
        return data


# --------------------------------------------------------------------------- publisher


class EventPublisher(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def publish(self, event: Event) -> None: ...
    async def healthy(self) -> None: ...
    @property
    def is_ready(self) -> bool: ...


class NullEventPublisher:
    """Used when Kafka is disabled (unit tests, local one-service runs)."""

    def __init__(self, service_name: str = "unknown") -> None:
        self.service_name = service_name
        self.published: list[Event] = []

    @property
    def is_ready(self) -> bool:
        return True

    async def start(self) -> None:
        logger.warning("kafka is disabled; events will be dropped", extra={"service": self.service_name})

    async def close(self) -> None:
        return None

    async def healthy(self) -> None:
        return None

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        logger.info("event discarded (kafka disabled)", extra={"event_type": event.event_type, "topic": event.topic})
        metrics.events_published_total.labels(self.service_name, event.topic, "dropped").inc()


class KafkaEventPublisher:
    def __init__(self, *, settings: KafkaSettings, service_name: str) -> None:
        self.settings = settings
        self.service_name = service_name
        self._producer: AIOKafkaProducer | None = None
        self._start_lock = asyncio.Lock()

    @property
    def is_ready(self) -> bool:
        return self._producer is not None

    async def start(self) -> None:
        if self._producer is not None:
            return
        async with self._start_lock:
            if self._producer is not None:
                return
            producer = AIOKafkaProducer(
                bootstrap_servers=self.settings.bootstrap_servers,
                client_id=f"{self.service_name}-producer",
                acks="all",
                enable_idempotence=True,  # broker-side de-duplication of retries
                linger_ms=5,
                max_batch_size=16384,
                request_timeout_ms=int(self.settings.publish_timeout_seconds * 1000),
                **self.settings.producer_extra,
            )
            await producer.start()
            self._producer = producer
            logger.info("kafka producer connected", extra={"bootstrap_servers": self.settings.bootstrap_servers})

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def healthy(self) -> None:
        if self._producer is None:
            raise DependencyUnavailableError("kafka producer is not connected")
        try:
            await asyncio.wait_for(
                self._producer.partitions_for(f"{self.service_name}-health"), timeout=self.settings.publish_timeout_seconds
            )
        except (TimeoutError, KafkaError) as exc:
            raise DependencyUnavailableError(f"kafka broker unreachable: {type(exc).__name__}") from exc

    async def publish(self, event: Event) -> None:
        if self._producer is None:
            raise DependencyUnavailableError(
                "kafka producer is not connected", code="broker_unavailable", details={"topic": event.topic}
            )
        try:
            await self._producer.send_and_wait(
                event.topic, key=event.message_key(), value=event.to_wire(), headers=event.kafka_headers()
            )
        except (TimeoutError, KafkaError) as exc:
            metrics.events_published_total.labels(self.service_name, event.topic, "failure").inc()
            logger.error("event publication failed", extra={"topic": event.topic, "event_type": event.event_type, "error": str(exc)})
            raise DependencyUnavailableError(
                f"failed to publish {event.event_type} to kafka: {type(exc).__name__}",
                code="broker_unavailable",
                details={"topic": event.topic, "event_id": event.event_id},
            ) from exc
        metrics.events_published_total.labels(self.service_name, event.topic, "success").inc()


# --------------------------------------------------------------------------- consumer

EventHandler = Callable[[Event], Awaitable[None]]


class EventConsumer:
    """Long running consumer: at-least-once delivery, retries, DLQ, graceful stop."""

    def __init__(
        self,
        *,
        settings: KafkaSettings,
        service_name: str,
        group_id: str,
        topics: Sequence[str],
        handler: EventHandler,
        on_dlq: Callable[[Event, Exception, int], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.service_name = service_name
        self.group_id = group_id
        self.topics = list(topics)
        self.handler = handler
        self.on_dlq = on_dlq
        self._consumer: AIOKafkaConsumer | None = None
        self._stop = asyncio.Event()
        self.stats: dict[str, int] = {"processed": 0, "duplicates": 0, "retries": 0, "dlq": 0, "failed": 0}

    @property
    def is_ready(self) -> bool:
        return self._consumer is not None and bool(self._consumer.assignment())

    async def start(self) -> None:
        consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self.settings.bootstrap_servers,
            group_id=self.group_id,
            client_id=f"{self.service_name}-consumer",
            enable_auto_commit=False,  # we commit only after the handler succeeded
            auto_offset_reset=self.settings.auto_offset_reset,
            max_poll_records=50,
            **self.settings.producer_extra,
        )
        await consumer.start()
        self._consumer = consumer
        logger.info("kafka consumer started", extra={"group_id": self.group_id, "topics": self.topics})

    async def stop(self) -> None:
        self._stop.set()
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    async def healthy(self) -> None:
        if self._consumer is None:
            raise DependencyUnavailableError("kafka consumer is not running")
        if not self._consumer.assignment():
            raise DependencyUnavailableError("kafka consumer has no partition assignment")

    async def run(self) -> None:
        if self._consumer is None:
            raise RuntimeError("EventConsumer.start() must be awaited before run()")
        while not self._stop.is_set():
            try:
                batches = await self._consumer.getmany(timeout_ms=int(self.settings.consume_timeout_seconds * 1000))
            except KafkaError as exc:
                logger.error("kafka poll failed", extra={"error": str(exc), "topics": self.topics})
                await asyncio.sleep(1.0)
                continue
            for topic_partition, messages in batches.items():
                for message in messages:
                    await self._handle(topic_partition, message)
        logger.info("kafka consumer stopped", extra={"group_id": self.group_id, "stats": self.stats})

    async def _handle(self, topic_partition: TopicPartition, message: Any) -> None:
        assert self._consumer is not None
        raw_headers = {key: (value.decode("utf-8") if value else "") for key, value in (message.headers or [])}
        try:
            event = Event.from_message(
                topic=message.topic,
                raw=message.value,
                headers=raw_headers,
                key=message.key.decode("utf-8") if message.key else None,
            )
        except ValueError as exc:
            logger.error("discarding malformed message", extra={"topic": message.topic, "offset": message.offset, "error": str(exc)})
            self.stats["failed"] += 1
            await self._commit(message)
            return

        with context.bind_context(correlation_id=event.correlation_id or context.get_correlation_id()):
            attempts = max(1, self.settings.max_handler_attempts)
            for attempt in range(1, attempts + 1):
                try:
                    await self.handler(event)
                except EventAlreadyProcessed:
                    self.stats["duplicates"] += 1
                    metrics.events_consumed_total.labels(self.service_name, event.topic, "duplicate").inc()
                    logger.info(
                        "event already applied, skipping",
                        extra={"topic": event.topic, "event_id": event.event_id, "event_type": event.event_type},
                    )
                    break
                except Exception as exc:
                    is_last = attempt >= attempts
                    logger.warning(
                        "event handler failed" if not is_last else "event handler exhausted retries",
                        extra={
                            "topic": event.topic,
                            "event_type": event.event_type,
                            "event_id": event.event_id,
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                    if is_last:
                        await self._dead_letter(event, exc, attempt)
                        self.stats["failed"] += 1
                    else:
                        self.stats["retries"] += 1
                        await asyncio.sleep(self.settings.handler_backoff_seconds * attempt)
                    continue
                else:
                    self.stats["processed"] += 1
                    metrics.events_consumed_total.labels(self.service_name, event.topic, "ok").inc()
                    break
            await self._commit(message)

    async def _commit(self, message: Any) -> None:
        assert self._consumer is not None
        topic_partition = TopicPartition(message.topic, message.partition)
        await self._consumer.commit({topic_partition: message.offset + 1})

    async def _dead_letter(self, event: Event, error: Exception, attempts: int) -> None:
        self.stats["dlq"] += 1
        metrics.events_consumed_total.labels(self.service_name, event.topic, "dlq").inc()
        try:
            if self.on_dlq is not None:
                await self.on_dlq(event, error, attempts)
        except Exception as exc:
            logger.error("failed to park event in the dead letter queue", extra={"event_id": event.event_id, "error": str(exc)})
