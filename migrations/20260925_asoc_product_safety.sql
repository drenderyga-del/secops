-- Keep ASOC product lifecycle rows and confirmed destructive-sync evidence
-- safe across a rollback to a release that still physically deletes missing
-- product cards. The guard FK intentionally rejects that deletion.
-- Block active sync transactions until the guard and insert trigger commit.
LOCK TABLE asoc_products IN ACCESS EXCLUSIVE MODE;

CREATE TABLE IF NOT EXISTS asoc_product_lifecycle_guards (
    asoc_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_asoc_lifecycle_guard_product
        FOREIGN KEY (asoc_id) REFERENCES asoc_products (asoc_id) ON DELETE RESTRICT
);

INSERT INTO asoc_product_lifecycle_guards (asoc_id)
SELECT asoc_id FROM asoc_products
ON CONFLICT (asoc_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS asoc_product_sync_events (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor VARCHAR(120) NOT NULL DEFAULT 'operator',
    previous_active_count INTEGER NOT NULL CHECK (previous_active_count >= 0),
    incoming_count INTEGER NOT NULL CHECK (incoming_count >= 0),
    removed_count INTEGER NOT NULL CHECK (removed_count >= 0),
    registry_version VARCHAR(64) NOT NULL
);

CREATE OR REPLACE FUNCTION ensure_asoc_product_lifecycle_guard()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO asoc_product_lifecycle_guards (asoc_id)
    VALUES (NEW.asoc_id)
    ON CONFLICT (asoc_id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_asoc_product_lifecycle_guard ON asoc_products;
CREATE TRIGGER trg_asoc_product_lifecycle_guard
AFTER INSERT ON asoc_products
FOR EACH ROW EXECUTE FUNCTION ensure_asoc_product_lifecycle_guard();

-- An older application release stores raw ASOC Description text and does not
-- enforce operator-only synchronization. After this migration, reject its
-- ASOC writes even if the app is rolled back. The lifecycle-aware release sets
-- this transaction-local marker only after validating and sanitizing a full
-- snapshot; it resets automatically at commit/rollback.
CREATE OR REPLACE FUNCTION require_asoc_product_lifecycle_write()
RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('scanops.asoc_product_lifecycle_write', true)
        IS DISTINCT FROM 'allowed' THEN
        RAISE EXCEPTION 'ASOC product writes require a lifecycle-aware ScanOps release'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_require_asoc_product_lifecycle_write ON asoc_products;
CREATE TRIGGER trg_require_asoc_product_lifecycle_write
BEFORE INSERT OR UPDATE ON asoc_products
FOR EACH ROW EXECUTE FUNCTION require_asoc_product_lifecycle_write();
