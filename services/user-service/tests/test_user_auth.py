"""Registration, login, lockout, token rotation and profile management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest

from tests.support.harness import TEST_JWT_SECRET

REGISTER = "/auth/register"
LOGIN = "/auth/login"
REFRESH = "/auth/refresh"
LOGOUT = "/auth/logout"

PASSWORD = "correct-horse-battery"


def credentials(email: str | None = None, password: str = PASSWORD, **extra: Any) -> dict[str, Any]:
    return {"email": email or f"user-{uuid.uuid4().hex[:8]}@example.com", "password": password, **extra}


async def register_user(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post(REGISTER, json={**credentials(), **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_register_returns_profile_and_tokens(client: httpx.AsyncClient) -> None:
    body = await register_user(client, full_name="Ada Lovelace")

    assert body["user"]["email"].startswith("user-")
    assert body["user"]["roles"] == ["customer"]
    assert body["user"]["status"] == "active"
    assert body["tokens"]["token_type"] == "Bearer"
    assert body["tokens"]["expires_in"] == 900
    assert len(body["tokens"]["refresh_token"]) > 30

    me = await client.get("/users/me", headers=bearer(body["tokens"]["access_token"]))
    assert me.status_code == 200
    assert me.json()["full_name"] == "Ada Lovelace"


async def test_register_rejects_duplicates_and_weak_passwords(client: httpx.AsyncClient) -> None:
    email = f"dupe-{uuid.uuid4().hex[:8]}@example.com"
    assert (await client.post(REGISTER, json=credentials(email))).status_code == 201

    duplicate = await client.post(REGISTER, json=credentials(email))
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "email_already_registered"

    short = await client.post(REGISTER, json=credentials(password="short1"))
    assert short.status_code == 422
    assert short.json()["error"]["code"] == "validation_error"

    allcaps = await client.post(REGISTER, json=credentials(password="1234567890!"))
    assert allcaps.status_code == 422
    assert allcaps.json()["error"]["code"] == "weak_password"
    assert allcaps.json()["error"]["details"]["field"] == "password"

    bad_email = await client.post(REGISTER, json=credentials("not-an-email"))
    assert bad_email.status_code == 422


async def test_login_success_and_failure_paths(client: httpx.AsyncClient) -> None:
    email = f"login-{uuid.uuid4().hex[:8]}@example.com"
    await client.post(REGISTER, json=credentials(email))

    ok = await client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["email"] == email

    unknown = await client.post(LOGIN, json={"email": f"ghost-{uuid.uuid4().hex[:6]}@example.com", "password": PASSWORD})
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "invalid_credentials"

    wrong = await client.post(LOGIN, json={"email": email, "password": "wrong-password-1"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["details"]["attempts_remaining"] == 4


async def test_account_locks_after_repeated_failures(client: httpx.AsyncClient) -> None:
    email = f"lock-{uuid.uuid4().hex[:8]}@example.com"
    await register_user(client, email=email)

    for expected_remaining in (4, 3, 2, 1):
        response = await client.post(LOGIN, json={"email": email, "password": "nope-nope-1"})
        assert response.status_code == 401
        assert response.json()["error"]["details"]["attempts_remaining"] == expected_remaining

    locked = await client.post(LOGIN, json={"email": email, "password": "nope-nope-1"})
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"
    assert int(locked.headers["Retry-After"]) > 0

    # Even the correct password is refused while the account is locked.
    still_locked = await client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert still_locked.status_code == 429
    assert still_locked.json()["error"]["details"]["retry_after_seconds"] > 0


async def test_access_token_validation_errors(client: httpx.AsyncClient) -> None:
    missing = await client.get("/users/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert missing.headers["WWW-Authenticate"] == "Bearer"

    garbage = await client.get("/users/me", headers=bearer("not.a.jwt"))
    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "invalid_token"

    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "email": "expired@example.com",
            "roles": ["customer"],
            "iss": "ecommerce.user-service",
            "aud": "ecommerce",
            "iat": int((now - timedelta(hours=3)).timestamp()),
            "nbf": int((now - timedelta(hours=3)).timestamp()),
            "exp": int((now - timedelta(hours=2)).timestamp()),
            "jti": uuid.uuid4().hex,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )
    stale = await client.get("/users/me", headers=bearer(expired))
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "token_expired"

    foreign = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "x@example.com", "iss": "somebody-else", "aud": "ecommerce", "exp": int((now + timedelta(hours=1)).timestamp())},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )
    rejected = await client.get("/users/me", headers=bearer(foreign))
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "invalid_token"


async def test_refresh_rotates_and_detects_reuse(client: httpx.AsyncClient) -> None:
    body = await register_user(client)
    original_refresh = body["tokens"]["refresh_token"]
    original_access = body["tokens"]["access_token"]

    rotated = await client.post(REFRESH, json={"refresh_token": original_refresh})
    assert rotated.status_code == 200, rotated.text
    new_refresh = rotated.json()["tokens"]["refresh_token"]
    assert new_refresh != original_refresh

    # Replaying the rotated token means it leaked: the whole family is revoked.
    replay = await client.post(REFRESH, json={"refresh_token": original_refresh})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reused"

    successor = await client.post(REFRESH, json={"refresh_token": new_refresh})
    assert successor.status_code == 401
    assert successor.json()["error"]["code"] == "refresh_token_reused"

    # Access tokens are stateless and keep working until they expire; that is why
    # the access token TTL is short.
    still_valid = await client.get("/users/me", headers=bearer(original_access))
    assert still_valid.status_code == 200

    unknown = await client.post(REFRESH, json={"refresh_token": "x" * 64})
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "invalid_refresh_token"


async def test_logout_revokes_sessions(client: httpx.AsyncClient) -> None:
    first = await register_user(client)
    email = first["user"]["email"]
    second = (await client.post(LOGIN, json={"email": email, "password": PASSWORD})).json()

    single = await client.post(
        LOGOUT, json={"refresh_token": first["tokens"]["refresh_token"]}, headers=bearer(first["tokens"]["access_token"])
    )
    assert single.status_code == 200
    assert single.json() == {"revoked_tokens": 1, "all_sessions": False}
    assert (await client.post(REFRESH, json={"refresh_token": first["tokens"]["refresh_token"]})).status_code == 401

    everything = await client.post("/auth/logout", json={"all_sessions": True}, headers=bearer(second["tokens"]["access_token"]))
    assert everything.json()["revoked_tokens"] >= 1
    assert (await client.post(REFRESH, json={"refresh_token": second["tokens"]["refresh_token"]})).status_code == 401


async def test_password_change_revokes_every_session(client: httpx.AsyncClient) -> None:
    body = await register_user(client)
    access = body["tokens"]["access_token"]

    wrong = await client.post(
        "/users/me/password",
        json={"current_password": "not-the-password", "new_password": "brand-new-secret-1"},
        headers=bearer(access),
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_credentials"

    same = await client.post(
        "/users/me/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD},
        headers=bearer(access),
    )
    assert same.status_code == 422
    assert same.json()["error"]["code"] == "password_unchanged"

    changed = await client.post(
        "/users/me/password",
        json={"current_password": PASSWORD, "new_password": "brand-new-secret-1"},
        headers=bearer(access),
    )
    assert changed.status_code == 200
    assert changed.json()["revoked_tokens"] == 1

    assert (await client.post(REFRESH, json={"refresh_token": body["tokens"]["refresh_token"]})).status_code == 401
    assert (await client.post(LOGIN, json={"email": body["user"]["email"], "password": PASSWORD})).status_code == 401
    assert (
        await client.post(LOGIN, json={"email": body["user"]["email"], "password": "brand-new-secret-1"})
    ).status_code == 200


async def test_profile_update(client: httpx.AsyncClient) -> None:
    body = await register_user(client)
    access = body["tokens"]["access_token"]

    updated = await client.patch("/users/me", json={"full_name": "Grace Hopper"}, headers=bearer(access))
    assert updated.status_code == 200
    assert updated.json()["full_name"] == "Grace Hopper"

    invalid = await client.patch("/users/me", json={"full_name": ""}, headers=bearer(access))
    assert invalid.status_code == 422

    from tests.support.harness import issue_token

    unknown_user = issue_token(user_id=str(uuid.uuid4()), secret=TEST_JWT_SECRET)
    missing = await client.get("/users/me", headers=bearer(unknown_user))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "user_not_found"


async def test_admin_listing_and_account_status(client: httpx.AsyncClient) -> None:
    from tests.support.harness import admin_token

    victim = await register_user(client, full_name="Victim Person")
    admin = admin_token(TEST_JWT_SECRET)
    customer = victim["tokens"]["access_token"]

    forbidden = await client.get("/users", headers=bearer(customer))
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    listing = await client.get("/users", params={"q": "Victim"}, headers=bearer(admin))
    assert listing.status_code == 200
    assert listing.json()["meta"]["total"] == 1
    assert listing.json()["items"][0]["id"] == victim["user"]["id"]

    by_role = await client.get("/users", params={"role": "customer"}, headers=bearer(admin))
    assert by_role.json()["meta"]["total"] >= 1

    fetched = await client.get(f"/users/{victim['user']['id']}", headers=bearer(admin))
    assert fetched.status_code == 200

    disabled = await client.post(
        f"/users/{victim['user']['id']}/status", json={"status": "disabled", "reason": "fraud"}, headers=bearer(admin)
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    blocked = await client.post(LOGIN, json={"email": victim["user"]["email"], "password": PASSWORD})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "account_disabled"

    # their refresh token died with the account
    assert (await client.post(REFRESH, json={"refresh_token": victim["tokens"]["refresh_token"]})).status_code == 403

    enabled = await client.post(f"/users/{victim['user']['id']}/status", json={"status": "active"}, headers=bearer(admin))
    assert enabled.json()["status"] == "active"
    assert (await client.post(LOGIN, json={"email": victim["user"]["email"], "password": PASSWORD})).status_code == 200

    # An operator acting on their own account is refused (compare the JWT subject
    # with the target id).
    from tests.support.harness import issue_token

    self_admin = issue_token(user_id=victim["user"]["id"], roles=("admin",), secret=TEST_JWT_SECRET)
    refused = await client.post(f"/users/{victim['user']['id']}/status", json={"status": "disabled"}, headers=bearer(self_admin))
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "self_disable_forbidden"


async def test_internal_token_verification_endpoint(client: httpx.AsyncClient, internal_headers: dict[str, str]) -> None:
    body = await register_user(client)
    access = body["tokens"]["access_token"]

    unauthenticated = await client.post("/internal/auth/verify", json={"token": access})
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "invalid_internal_token"

    verified = await client.post("/internal/auth/verify", json={"token": access}, headers=internal_headers)
    assert verified.status_code == 200, verified.text
    payload = verified.json()
    assert payload["valid"] is True
    assert payload["user_id"] == body["user"]["id"]
    assert payload["roles"] == ["customer"]

    rejected = await client.post("/internal/auth/verify", json={"token": "not-a-valid-jwt-but-long-enough"}, headers=internal_headers)
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "invalid_token"


async def test_bootstrap_admin_is_created_on_startup(user_database: tuple[str, str]) -> None:
    from user_service.main import build_app
    from user_service.settings import UserServiceSettings

    from tests.support.harness import DEFAULT_ADMIN_DSN

    _, url = user_database
    settings = UserServiceSettings(
        database_url=url,
        environment="test",
        jwt_secret=TEST_JWT_SECRET,
        internal_service_token="test-internal-token",
        bcrypt_rounds=4,
        log_format="console",
        log_level="WARNING",
        bootstrap_admin_email="root@example.com",
        bootstrap_admin_password="RootPass123!",
    )
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://user-service") as client:
            login = await client.post(LOGIN, json={"email": "root@example.com", "password": "RootPass123!"})
            assert login.status_code == 200, login.text
            assert login.json()["user"]["roles"] == ["admin"]
    del DEFAULT_ADMIN_DSN


async def test_health_and_metrics(client: httpx.AsyncClient) -> None:
    assert (await client.get("/live")).json()["status"] == "alive"
    ready = await client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"]["status"] == "ok"

    metrics = await client.get("/metrics")
    assert "user_logins_total" in metrics.text
    assert "service_http_requests_total" in metrics.text


@pytest.mark.parametrize("path", ["/auth/login", "/auth/register", "/auth/refresh"])
async def test_auth_endpoints_validate_bodies(client: httpx.AsyncClient, path: str) -> None:
    response = await client.post(path, json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
