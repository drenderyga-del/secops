-- Persist the repository-backed ASOC-to-ScanOps product mapping projection.
ALTER TABLE asoc_products ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE asoc_products ADD COLUMN IF NOT EXISTS repositories_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE asoc_products ADD COLUMN IF NOT EXISTS matched_repositories_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE asoc_products ADD COLUMN IF NOT EXISTS mapping_state VARCHAR(32) NOT NULL DEFAULT 'not_synced';
ALTER TABLE asoc_products ADD COLUMN IF NOT EXISTS mapping_detail TEXT;
ALTER TABLE asoc_products ADD COLUMN IF NOT EXISTS monitor_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_asoc_products_mapping_state ON asoc_products (mapping_state);
CREATE INDEX IF NOT EXISTS ix_asoc_products_monitor_product_id ON asoc_products (monitor_product_id);
