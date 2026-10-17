-- Optional product imagery for the storefront.
-- NULL means "no image": clients fall back to a generated tile, so no backfill
-- is required for existing rows.

ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url VARCHAR(500);
