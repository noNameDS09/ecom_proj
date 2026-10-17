"""Prometheus metrics, exposed on ``/metrics``."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# A dedicated registry keeps tests isolated from the global default registry.
REGISTRY = CollectorRegistry(auto_describe=True)

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

http_requests_total = Counter(
    "service_http_requests_total",
    "HTTP requests handled, by service/route/status.",
    labelnames=("service", "method", "route", "status"),
    registry=REGISTRY,
)
http_request_duration_seconds = Histogram(
    "service_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("service", "method", "route"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
http_errors_total = Counter(
    "service_http_errors_total",
    "HTTP responses with a status >= 500.",
    labelnames=("service", "route", "error_code"),
    registry=REGISTRY,
)
upstream_requests_total = Counter(
    "service_upstream_requests_total",
    "Outbound calls to other services.",
    labelnames=("service", "target", "outcome"),
    registry=REGISTRY,
)
upstream_duration_seconds = Histogram(
    "service_upstream_request_duration_seconds",
    "Outbound call latency in seconds.",
    labelnames=("service", "target"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
circuit_breaker_state = Gauge(
    "service_circuit_breaker_open",
    "1 when the outbound circuit breaker for a target is open.",
    labelnames=("service", "target"),
    registry=REGISTRY,
)
events_published_total = Counter(
    "service_events_published_total",
    "Domain events handed to the broker.",
    labelnames=("service", "topic", "outcome"),
    registry=REGISTRY,
)
events_consumed_total = Counter(
    "service_events_consumed_total",
    "Domain events consumed, by outcome (ok/retry/duplicate/dlq).",
    labelnames=("service", "topic", "outcome"),
    registry=REGISTRY,
)
outbox_backlog = Gauge(
    "service_outbox_backlog",
    "Events stored in the transactional outbox awaiting publication.",
    labelnames=("service",),
    registry=REGISTRY,
)
outbox_publish_failures_total = Counter(
    "service_outbox_publish_failures_total",
    "Outbox publish attempts that failed.",
    labelnames=("service",),
    registry=REGISTRY,
)
readiness_status = Gauge(
    "service_readiness_status",
    "1 when the last readiness probe for a dependency succeeded.",
    labelnames=("service", "dependency"),
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def domain_counter(metric_name: str, description: str, labels: tuple[str, ...] = ()) -> Counter:
    """Register a service-specific counter on the shared registry."""
    return Counter(metric_name, description, labelnames=labels, registry=REGISTRY)


def domain_gauge(metric_name: str, description: str, labels: tuple[str, ...] = ()) -> Gauge:
    return Gauge(metric_name, description, labelnames=labels, registry=REGISTRY)


def domain_histogram(metric_name: str, description: str, labels: tuple[str, ...] = (), buckets: tuple[float, ...] = ()) -> Histogram:
    return Histogram(
        metric_name,
        description,
        labelnames=labels,
        buckets=buckets or (0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
        registry=REGISTRY,
    )
