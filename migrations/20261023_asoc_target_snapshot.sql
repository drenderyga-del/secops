-- Upgrade early local previews as well as fresh installations. Historical
-- receipts without target provenance cannot attest to the current destination.
ALTER TABLE asoc_destinations ADD COLUMN IF NOT EXISTS config_version VARCHAR(36) NOT NULL DEFAULT 'legacy-unknown';
ALTER TABLE asoc_report_deliveries ADD COLUMN IF NOT EXISTS config_version VARCHAR(36) NOT NULL DEFAULT 'legacy-unknown';
ALTER TABLE asoc_report_deliveries ADD COLUMN IF NOT EXISTS target_json TEXT NOT NULL DEFAULT '{}';
UPDATE asoc_report_deliveries SET state = 'unknown' WHERE target_json = '{}';
