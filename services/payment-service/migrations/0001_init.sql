-- Payments, one row per charged order (order_id is unique: that constraint *is*
-- the idempotency guarantee against double charging).
--
-- The outbox/processed/failed tables are the same eventing trio used by the
-- other services: payment.events are written in the same transaction as the
-- payment row and published by the relay.

CREATE TABLE payments (
    id                 UUID PRIMARY KEY,
    order_id           UUID         NOT NULL,
    order_number       VARCHAR(32)  NOT NULL,
    customer_email     VARCHAR(320) NOT NULL,
    description        VARCHAR(200),
    amount             NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    currency           CHAR(3)      NOT NULL,
    status             VARCHAR(16)  NOT NULL CHECK (status IN ('succeeded', 'failed')),
    provider           VARCHAR(40)  NOT NULL,
    provider_reference VARCHAR(64),
    failure_reason     VARCHAR(120),
    source             VARCHAR(20)  NOT NULL DEFAULT 'api' CHECK (source IN ('api', 'kafka', 'system')),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_payments_order_id ON payments (order_id);
CREATE INDEX ix_payments_status_created_at ON payments (status, created_at DESC);
CREATE INDEX ix_payments_created_at ON payments (created_at DESC, id DESC);

CREATE TABLE outbox_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        VARCHAR(64)  NOT NULL,
    topic           VARCHAR(200) NOT NULL,
    event_type      VARCHAR(120) NOT NULL,
    partition_key   VARCHAR(200),
    payload         JSONB        NOT NULL,
    headers         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ  NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    claimed_at      TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    attempts        INTEGER      NOT NULL DEFAULT 0,
    status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
    last_error      TEXT
);

CREATE UNIQUE INDEX uq_outbox_events_event_id ON outbox_events (event_id);
CREATE INDEX ix_outbox_events_status_next_attempt_at ON outbox_events (status, next_attempt_at);

CREATE TABLE processed_events (
    event_id     VARCHAR(64)  NOT NULL,
    consumer     VARCHAR(120) NOT NULL,
    event_type   VARCHAR(120) NOT NULL,
    processed_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT pk_processed_events PRIMARY KEY (event_id, consumer)
);

CREATE TABLE failed_events (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id   VARCHAR(64)  NOT NULL,
    consumer   VARCHAR(120) NOT NULL,
    topic      VARCHAR(200) NOT NULL,
    event_type VARCHAR(120) NOT NULL,
    payload    JSONB        NOT NULL,
    error      TEXT         NOT NULL,
    attempts   INTEGER      NOT NULL DEFAULT 1,
    failed_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX ix_failed_events_event_id ON failed_events (event_id);
