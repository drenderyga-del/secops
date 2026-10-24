-- Preserve product lifecycle history when an ASOC card disappears from a
-- complete product snapshot. Existing rows are active until first observed
-- missing by a successful synchronization.
ALTER TABLE asoc_products
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE asoc_products
    ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_asoc_products_is_active
    ON asoc_products (is_active);
