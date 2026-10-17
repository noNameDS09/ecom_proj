"""Base configuration shared by all services (12-factor: everything from env)."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KafkaSettings(BaseSettings):
    """Kafka client configuration (prefixed with ``KAFKA_``)."""

    model_config = SettingsConfigDict(env_prefix="KAFKA_", extra="ignore", case_sensitive=False)

    enabled: bool = True
    bootstrap_servers: str = "localhost:9092"
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None
    publish_timeout_seconds: float = 10.0
    consume_timeout_seconds: float = 1.0
    auto_offset_reset: str = "earliest"
    consumer_group_prefix: str = "ecommerce"
    max_handler_attempts: int = 4
    handler_backoff_seconds: float = 0.5
    dlq_topic: str = "ecommerce.dlq"
    outbox_poll_interval_seconds: float = 0.5
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 8
    outbox_backoff_base_seconds: float = 0.5
    outbox_backoff_max_seconds: float = 30.0

    @property
    def producer_extra(self) -> dict[str, Any]:
        extra: dict[str, Any] = {"security_protocol": self.security_protocol}
        if self.sasl_mechanism:
            extra.update(
                sasl_mechanism=self.sasl_mechanism,
                sasl_plain_username=self.sasl_username,
                sasl_plain_password=self.sasl_password,
            )
        return extra


class Settings(BaseSettings):
    """Common service settings. Service specific settings subclass this."""

    model_config = SettingsConfigDict(env_file=(".env",), extra="ignore", case_sensitive=False)

    service_name: str = "service"
    service_version: str = "0.1.0"
    environment: str = "local"  # local | test | staging | production
    debug: bool = False

    # HTTP server
    host: str = "0.0.0.0"
    port: int = 8000

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # Database
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout_seconds: float = 10.0
    db_pool_recycle_seconds: int = 1800
    db_statement_timeout_ms: int = 15000
    db_echo: bool = False
    db_connect_attempts: int = 30
    db_connect_retry_delay_seconds: float = 1.0

    # Migrations
    run_migrations: bool = True
    migrations_dir: str = "migrations"

    # Auth (HS256 shared secret: user-service signs, the rest verify)
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "ecommerce.user-service"
    jwt_audience: str = "ecommerce"
    access_token_ttl_seconds: int = 3600
    password_min_length: int = 8
    bcrypt_rounds: int = 12

    # Observability
    metrics_enabled: bool = True
    access_log_enabled: bool = True

    # Service-to-service authentication. Production would use mTLS/SPIFFE or
    # OAuth2 client credentials; a shared secret keeps the demo self contained.
    internal_service_token: str = "dev-internal-token"

    # Lifecycle
    shutdown_grace_seconds: float = 20.0
    readiness_timeout_seconds: float = 3.0

    # Kafka (nested: KAFKA_BOOTSTRAP_SERVERS, ...)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)

    @property
    def is_local(self) -> bool:
        return self.environment in {"local", "test"}

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL (``postgresql+asyncpg://``)."""
        return self.database_url

    @property
    def asyncpg_dsn(self) -> str:
        """Plain libpq URL used by the migration runner / admin tooling."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("log_format")
    @classmethod
    def _normalise_format(cls, value: str) -> str:
        normalised = value.lower()
        if normalised not in {"json", "console"}:
            raise ValueError("log_format must be 'json' or 'console'")
        return normalised


S = TypeVar("S", bound=Settings)


def load_settings(settings_cls: type[S]) -> S:
    """Instantiate settings once per process (services call this in ``main``)."""
    return settings_cls()
