-- ASOC product cards synced via GET /ext/v1/products/ for coverage reports.
-- IF NOT EXISTS keeps this idempotent with Base.metadata.create_all on fresh DBs.
CREATE TABLE IF NOT EXISTS asoc_products (
    id SERIAL PRIMARY KEY,
    asoc_id VARCHAR NOT NULL UNIQUE,
    name VARCHAR NOT NULL,
    slug VARCHAR,
    bi_id VARCHAR(32),
    title VARCHAR,
    asoc_created TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_asoc_products_asoc_id ON asoc_products (asoc_id);
CREATE INDEX IF NOT EXISTS ix_asoc_products_slug ON asoc_products (slug);
CREATE INDEX IF NOT EXISTS ix_asoc_products_bi_id ON asoc_products (bi_id);
