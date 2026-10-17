-- Identity schema: users + rotating refresh tokens.
--
-- Design notes
--   * only the SHA-256 of a refresh token is stored, so a database dump cannot be
--     replayed against the API;
--   * refresh tokens belong to a "family"; presenting a revoked token means the
--     token leaked, and the whole family is revoked (reuse detection);
--   * failed logins are counted on the row and lock the account temporarily,
--     which blunts credential stuffing without a separate rate limiter.

CREATE TABLE users (
    id                   UUID PRIMARY KEY,
    email                VARCHAR(320) NOT NULL,
    password_hash        VARCHAR(255) NOT NULL,
    full_name            VARCHAR(200) NOT NULL DEFAULT '',
    roles                TEXT[]       NOT NULL DEFAULT '{customer}',
    status               VARCHAR(20)  NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'disabled')),
    failed_login_attempts INTEGER     NOT NULL DEFAULT 0,
    locked_until         TIMESTAMPTZ,
    last_login_at        TIMESTAMPTZ,
    password_changed_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ
);

CREATE UNIQUE INDEX uq_users_email ON users (lower(email)) WHERE deleted_at IS NULL;
CREATE INDEX ix_users_created_at ON users (created_at DESC, id DESC);
CREATE INDEX ix_users_roles ON users USING gin (roles);

CREATE TABLE refresh_tokens (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    family_id     UUID NOT NULL,
    token_hash    VARCHAR(64) NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    revoked_reason VARCHAR(60),
    replaced_by   UUID,
    user_agent    VARCHAR(300),
    ip_address    VARCHAR(64),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_refresh_tokens_token_hash ON refresh_tokens (token_hash);
CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE INDEX ix_refresh_tokens_family_id ON refresh_tokens (family_id);
CREATE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (expires_at);
