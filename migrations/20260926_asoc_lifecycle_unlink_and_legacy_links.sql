-- 1. Deleting or merging a ScanOps product clears asoc_products.monitor_product_id
--    through ON DELETE SET NULL. PostgreSQL issues that UPDATE itself, outside
--    any ASOC sync, so the lifecycle write gate must let exactly that change
--    through; every other write still needs a lifecycle-aware release.
-- 2. Cards linked before first_connected_at was tracked keep no date. Record
--    that durably so a later unlink/relink never stamps a new connection date.
SET LOCAL lock_timeout = '15s';

ALTER TABLE asoc_products
    ADD COLUMN IF NOT EXISTS connected_before_tracking BOOLEAN NOT NULL DEFAULT FALSE;

CREATE OR REPLACE FUNCTION require_asoc_product_lifecycle_write()
RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('scanops.asoc_product_lifecycle_write', true) = 'allowed' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE'
        AND OLD.monitor_product_id IS NOT NULL
        AND NEW.monitor_product_id IS NULL
        AND (to_jsonb(NEW) - 'monitor_product_id') = (to_jsonb(OLD) - 'monitor_product_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'ASOC product writes require a lifecycle-aware ScanOps release'
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

SELECT set_config('scanops.asoc_product_lifecycle_write', 'allowed', true);

UPDATE asoc_products
SET connected_before_tracking = TRUE
WHERE first_connected_at IS NULL
  AND connected_before_tracking IS FALSE
  AND (
      monitor_product_id IS NOT NULL
      OR mapping_state IN ('mapped', 'partial')
      OR asoc_id IN (SELECT asoc_id FROM asoc_product_repository_mappings)
  );
