-- Record every ASOC sync that archived product cards, not only the mass
-- removals an operator confirmed. Rows written before this migration were all
-- operator confirmations, so the default keeps their meaning.
ALTER TABLE asoc_product_sync_events
    ADD COLUMN IF NOT EXISTS confirmed BOOLEAN NOT NULL DEFAULT TRUE;
