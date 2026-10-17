-- Orders, their items, the status audit trail and the eventing tables.
--
-- Orders denormalise the product data they were priced with (sku, name, unit
-- price). That is deliberate: an order is a record of what was agreed, and it
-- must not change when the catalogue later renames or reprices a product.

CREATE TABLE orders (
    id              UUID PRIMARY KEY,
    order_number    VARCHAR(32)  NOT NULL,
    user_id         UUID         NOT NULL,
    user_email      VARCHAR(320) NOT NULL,
    status          VARCHAR(24)  NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'paid', 'payment_failed', 'cancelled', 'fulfilled')),
    currency        CHAR(3)      NOT NULL DEFAULT 'USD',
    subtotal        NUMERIC(12,2) NOT NULL CHECK (subtotal >= 0),
    shipping_fee    NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (shipping_fee >= 0),
    total           NUMERIC(12,2) NOT NULL CHECK (total >= 0),
    item_count      INTEGER      NOT NULL CHECK (item_count > 0),
    shipping_address JSONB,
    notes           TEXT,
    idempotency_key VARCHAR(200),
    version         INTEGER      NOT NULL DEFAULT 1,
    paid_at         TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,
    cancel_reason   VARCHAR(200),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_orders_order_number ON orders (order_number);
CREATE INDEX ix_orders_user_id_created_at ON orders (user_id, created_at DESC);
CREATE INDEX ix_orders_status_created_at ON orders (status, created_at DESC);
CREATE INDEX ix_orders_created_at ON orders (created_at DESC, id DESC);

CREATE TABLE order_items (
    id          UUID PRIMARY KEY,
    order_id    UUID         NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id  UUID         NOT NULL,
    sku         VARCHAR(64)  NOT NULL,
    name        VARCHAR(200) NOT NULL,
    quantity    INTEGER      NOT NULL CHECK (quantity > 0),
    unit_price  NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    line_total  NUMERIC(12,2) NOT NULL CHECK (line_total > 0),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_order_items_order_product UNIQUE (order_id, product_id)
);

CREATE INDEX ix_order_items_order_id ON order_items (order_id);

-- Every status change is appended (who/what changed it and why): the audit trail
-- that makes the Kafka driven payment saga debuggable.
CREATE TABLE order_status_history (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    UUID        NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    from_status VARCHAR(24),
    to_status   VARCHAR(24) NOT NULL,
    reason      VARCHAR(200),
    source      VARCHAR(20) NOT NULL DEFAULT 'api' CHECK (source IN ('api', 'kafka', 'saga', 'system')),
    actor       VARCHAR(64),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_order_status_history_order_id_occurred_at ON order_status_history (order_id, occurred_at);

-- ---------------------------------------------------------------------------
-- Replayed writes (POST /orders carries an Idempotency-Key).
-- ---------------------------------------------------------------------------
CREATE TABLE idempotency_keys (
    key             VARCHAR(200) NOT NULL,
    endpoint        VARCHAR(200) NOT NULL,
    principal_id    VARCHAR(64)  NOT NULL DEFAULT 'anonymous',
    request_hash    VARCHAR(64)  NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'in_progress',
    response_status INTEGER,
    response_body   JSONB,
    resource_id     VARCHAR(64),
    claimed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ  NOT NULL,
    CONSTRAINT pk_idempotency_keys PRIMARY KEY (key, endpoint, principal_id)
);

CREATE INDEX ix_idempotency_keys_expires_at ON idempotency_keys (expires_at);

-- ---------------------------------------------------------------------------
-- Transactional outbox: order.created / order.cancelled are written in the same
-- transaction as the order row and published by the relay with at-least-once
-- semantics. processed_events makes the consumers idempotent.
-- ---------------------------------------------------------------------------
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
