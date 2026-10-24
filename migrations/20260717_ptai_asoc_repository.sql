BEGIN;

-- Needed to build a direct ASOC UI link:
-- /repositories/{repository_id}/scans/{scan_id}
ALTER TABLE ptai_scans
    ADD COLUMN IF NOT EXISTS asoc_repository_id VARCHAR;

COMMIT;
