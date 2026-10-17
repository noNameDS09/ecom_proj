-- Trigram index for ``GET /products?q=`` (ILIKE '%term%').
-- Guarded: pg_trgm ships with postgresql-contrib; if it is missing the service
-- still boots, searches just fall back to a sequential scan.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS ix_products_name_trgm ON products USING gin (name gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS ix_products_description_trgm ON products USING gin (description gin_trgm_ops);
    END IF;
END
$$;
