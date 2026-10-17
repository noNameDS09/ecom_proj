"""User service settings."""

from __future__ import annotations

from common.config import Settings


class UserServiceSettings(Settings):
    service_name: str = "user-service"
    service_version: str = "0.1.0"
    port: int = 8002
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/user_db"

    refresh_token_ttl_seconds: int = 14 * 24 * 3600
    max_failed_login_attempts: int = 5
    account_lock_seconds: int = 15 * 60
    default_role: str = "customer"

    # Demo convenience: create/refresh an admin account on boot so a fresh stack is
    # immediately usable. Disable by clearing these in production.
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "Admin123!"
    bootstrap_admin_full_name: str = "Platform Admin"
