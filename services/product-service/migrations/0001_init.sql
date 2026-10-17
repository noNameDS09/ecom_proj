-- Product catalogue and the stock reservation ledger.
-- Reservation model (two phase commit so a paid order can never oversell):
--   reserve  -> reserved += qty                      (available = stock - reserved)
--   release  -> reserved -= qty                      (order failed/cancelled)
--   commit   -> stock -= qty, reserved -= qty        (payment captured, goods ship)

CREATE TABLE products (
    id          UUID PRIMARY KEY,
    sku         VARCHAR(64)  NOT NULL,
    name        VARCHAR(200) NOT NULL,
    description TEXT         NOT NULL DEFAULT '',
    category    VARCHAR(80)  NOT NULL,
    price       NUMERIC(12,2) NOT NULL CHECK (price > 0),
    currency    CHAR(3)      NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
    stock       INTEGER      NOT NULL DEFAULT 0 CHECK (stock >= 0),
    reserved    INTEGER      NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    active      BOOLEAN      NOT NULL DEFAULT TRUE,
    version     INTEGER      NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ,
    CONSTRAINT ck_products_reserved_within_stock CHECK (reserved <= stock)
);

CREATE UNIQUE INDEX uq_products_sku_active ON products (sku) WHERE deleted_at IS NULL;
-- A soft-deleted SKU may be re-used, so the plain unique constraint is replaced by
-- the partial unique index above and kept for fast exact lookups of any state.
CREATE INDEX ix_products_sku ON products (sku);
CREATE INDEX ix_products_category ON products (category) WHERE deleted_at IS NULL;
CREATE INDEX ix_products_active_stock ON products (active, stock) WHERE deleted_at IS NULL;
CREATE INDEX ix_products_created_at ON products (created_at DESC, id DESC);

CREATE TABLE stock_reservations (
    id          UUID PRIMARY KEY,
    order_id    UUID        NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'released', 'committed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    committed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX uq_stock_reservations_order_id ON stock_reservations (order_id);
CREATE INDEX ix_stock_reservations_status ON stock_reservations (status);

CREATE TABLE stock_reservation_items (
    id             UUID PRIMARY KEY,
    reservation_id UUID    NOT NULL REFERENCES stock_reservations (id) ON DELETE CASCADE,
    product_id     UUID    NOT NULL REFERENCES products (id),
    sku            VARCHAR(64)   NOT NULL,
    name           VARCHAR(200)  NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    unit_price     NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_stock_reservation_items_reservation_product UNIQUE (reservation_id, product_id)
);

CREATE INDEX ix_stock_reservation_items_product_id ON stock_reservation_items (product_id);

INSERT INTO products (id, sku, name, description, category, price, currency, stock, active)
VALUES
    (gen_random_uuid(), 'SKU-KEYBOARD-001', 'Mechanical Keyboard', 'Tactile 87-key mechanical keyboard', 'peripherals', 129.00, 'USD', 40, TRUE),
    (gen_random_uuid(), 'SKU-MOUSE-001', 'Wireless Mouse', 'Ergonomic wireless mouse', 'peripherals', 59.50, 'USD', 120, TRUE),
    (gen_random_uuid(), 'SKU-MONITOR-001', '27" 4K Monitor', '27 inch 4K IPS monitor', 'displays', 429.99, 'USD', 15, TRUE),
    (gen_random_uuid(), 'SKU-HEADSET-001', 'Noise Cancelling Headset', 'Over-ear ANC headset', 'audio', 199.00, 'USD', 8, TRUE),
    (gen_random_uuid(), 'SKU-DOCK-001', 'USB-C Dock', '11-in-1 USB-C dock', 'accessories', 89.99, 'USD', 0, TRUE),
    (gen_random_uuid(), 'SKU-LEGACY-001', 'Retired Webcam', 'Discontinued 1080p webcam', 'video', 45.00, 'USD', 3, FALSE)
ON CONFLICT DO NOTHING;
