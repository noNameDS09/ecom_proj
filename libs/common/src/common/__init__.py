"""Shared runtime library for the e-commerce microservices.

Modules
-------
``config``       environment driven settings
``logging``      structured logging (JSON or console)
``context``      request/correlation/principal context propagation
``errors``       typed errors + uniform JSON error envelope
``middleware``   access logs and RED metrics as raw ASGI middleware
``health``       liveness/readiness/version endpoints
``db``           async SQLAlchemy engine, unit-of-work session, base models
``migrations``   boot-time SQL migration runner
``security``     bcrypt password hashing + JWT issue/verify
``auth``         FastAPI auth dependencies (principal, roles)
``httpclient``   resilient service-to-service HTTP client
``events``       Kafka producer/consumer with retries + DLQ
``outbox``       transactional outbox, consumer dedup, dead-letter storage
``idempotency``  Idempotency-Key support for unsafe writes
``schemas``      shared pydantic models (money, pagination, error envelope)
``app``          service bootstrap (create_app) and runtime lifecycle
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
