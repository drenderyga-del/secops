-- Preserve the first time a canonical ASOC card is confirmed against ScanOps
-- and the exact links that resolve it to one or more monitored product groups.
ALTER TABLE asoc_products
    ADD COLUMN IF NOT EXISTS first_connected_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS asoc_product_repository_mappings (
    id SERIAL PRIMARY KEY,
    asoc_id VARCHAR NOT NULL REFERENCES asoc_products(asoc_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    description_url VARCHAR(2048) NOT NULL,
    repository_id INTEGER REFERENCES repositories(id) ON DELETE SET NULL,
    CONSTRAINT uq_asoc_product_description_mapping UNIQUE (asoc_id, description_url)
);

CREATE INDEX IF NOT EXISTS ix_asoc_product_repository_mappings_asoc_id
    ON asoc_product_repository_mappings (asoc_id);
CREATE INDEX IF NOT EXISTS ix_asoc_product_repository_mappings_product_id
    ON asoc_product_repository_mappings (product_id);
CREATE INDEX IF NOT EXISTS ix_asoc_product_repository_mappings_repository_id
    ON asoc_product_repository_mappings (repository_id);
