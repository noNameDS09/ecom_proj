"""Structured (JSON) logging with request context and uvicorn integration."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from common import context

_RESERVED: frozenset[str] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName", "color_message",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders log records as single-line JSON objects."""

    def __init__(self, service: str, environment: str, version: str = "0.1.0") -> None:
        super().__init__()
        self.service = service
        self.environment = environment
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "env": self.environment,
            "version": self.version,
            "message": record.getMessage(),
            **context.snapshot(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _coerce(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human friendly formatter for local development."""

    COLORS = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[35m"}

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        reset = "\033[0m" if color else ""
        ctx = " ".join(f"{k}={v}" for k, v in context.snapshot().items())
        extras = " ".join(
            f"{k}={_coerce(v)}"
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        )
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} {color}{record.levelname:<8}{reset} "
            f"{record.name} {record.getMessage()}"
        )
        tail = " ".join(part for part in (ctx, extras) if part)
        if tail:
            base = f"{base} | {tail}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def _coerce(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


def configure_logging(
    *,
    service: str,
    version: str = "0.1.0",
    environment: str = "local",
    level: str = "INFO",
    fmt: str = "json",
) -> None:
    """Configure root, uvicorn and sqlalchemy loggers. Idempotent: re-calling replaces the handler."""
    root = logging.getLogger()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter: logging.Formatter = (
        ConsoleFormatter() if fmt == "console" else JsonFormatter(service=service, environment=environment, version=version)
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Keep third-party loggers readable but routed through our handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine", "aiokafka"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # replaced by AccessLogMiddleware
    # SQL statements are noisy (and echoed statements can leak data); keep them at WARNING.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
