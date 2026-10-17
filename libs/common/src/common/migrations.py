"""A tiny, dependency-free SQL migration runner.

Why not Alembic? These services own small, linear schemas and we want the
container to converge on boot with zero code generation. Migrations are plain
``.sql`` files executed in filename order, recorded in ``schema_migrations``
with a checksum so an edited - already applied - migration fails loudly.

Each migration runs inside its own transaction under a Postgres advisory lock,
so several replicas starting at once cannot double-apply it.

File naming: ``0001_init.sql``, ``0002_add_orders.sql`` ...
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import asyncpg

from common.errors import AppError
from common.logging import get_logger

logger = get_logger("common.migrations")

ADVISORY_LOCK_KEY = 8_915_342_211_007
_FILENAME_RE = re.compile(r"^(?P<version>\d+)[_-].*\.sql$")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER NOT NULL DEFAULT 0
)
"""


class MigrationError(AppError):
    status_code = 500
    code = "migration_failed"
    message = "Database migration failed"


def discover_migrations(directory: str | Path) -> list[Path]:
    path = Path(directory)
    if not path.is_dir():
        raise MigrationError(f"migrations directory not found: {path}")
    files = sorted((f for f in path.iterdir() if f.suffix == ".sql" and f.is_file()), key=lambda f: f.name)
    for file in files:
        if not _FILENAME_RE.match(file.name):
            raise MigrationError(f"invalid migration filename (expected NNNN_name.sql): {file.name}")
    return files


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def resolve_migrations_dir(configured: str, *, package_file: str) -> str:
    """Locate the migrations directory.

    Precedence: an explicit absolute path (env var) -> relative to the current
    working directory (``WORKDIR /app`` in the image) -> relative to the service
    root, derived from the calling module's location (``<service>/src/<pkg>/main.py``).
    That makes ``.sql`` migrations work identically in Docker, in tests and when
    the service is started from the repository root.
    """
    candidate = Path(configured)
    if candidate.is_absolute():
        return str(candidate)
    cwd_relative = Path.cwd() / candidate
    if cwd_relative.is_dir():
        return str(cwd_relative)
    service_root = Path(package_file).resolve().parents[2] / candidate
    if service_root.is_dir():
        return str(service_root)
    return str(candidate)


async def run_migrations(dsn: str, directory: str | Path, *, connect_timeout: float = 10.0) -> list[str]:
    """Apply pending migrations. Returns the versions applied in this call."""
    files = discover_migrations(directory)
    applied_now: list[str] = []
    connection = await asyncpg.connect(_libpq(dsn), timeout=connect_timeout)
    try:
        # Session level lock: released explicitly in the finally block.
        await connection.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        await connection.execute(_CREATE_TABLE)
        rows = await connection.fetch("SELECT version, checksum FROM schema_migrations")
        known = {row["version"]: row["checksum"] for row in rows}

        for file in files:
            version = file.name
            content = file.read_text(encoding="utf-8")
            checksum = _checksum(content)
            if version in known:
                if known[version] != checksum:
                    raise MigrationError(
                        f"migration {version} was modified after being applied "
                        f"(recorded checksum {known[version]}, file checksum {checksum}); "
                        "add a new migration instead"
                    )
                continue
            started = asyncio.get_running_loop().time()
            try:
                async with connection.transaction():
                    await connection.execute(content)
                    elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                    await connection.execute(
                        "INSERT INTO schema_migrations (version, checksum, duration_ms) VALUES ($1, $2, $3)",
                        version,
                        checksum,
                        elapsed_ms,
                    )
            except Exception as exc:
                raise MigrationError(f"failed to apply migration {version}: {exc}") from exc
            applied_now.append(version)
            logger.info("migration applied", extra={"migration": version, "duration_ms": elapsed_ms})

        if not applied_now:
            logger.info("database schema is up to date", extra={"migrations": len(files)})
    finally:
        try:
            await connection.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
        finally:
            await connection.close()
    return applied_now


async def applied_migrations(dsn: str) -> list[dict[str, object]]:
    connection = await asyncpg.connect(_libpq(dsn))
    try:
        await connection.execute(_CREATE_TABLE)
        rows = await connection.fetch("SELECT version, checksum, applied_at, duration_ms FROM schema_migrations ORDER BY version")
        return [dict(row) for row in rows]
    finally:
        await connection.close()


def _libpq(dsn: str) -> str:
    """``postgresql+asyncpg://`` is a SQLAlchemy spelling; asyncpg wants plain URLs."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")
