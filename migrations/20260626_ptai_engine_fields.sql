BEGIN;

-- Direct PT AI engine status, resolved by project name (`vampy-asoc-<repo slug>`)
-- + branch. ASOC gives the orchestration-level status; PT AI gives the scan
-- engine's stage/progress and its own errors.
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_project_id VARCHAR;
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_branch_id VARCHAR;
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_scan_result_id VARCHAR;
-- PT AI Stage enum: Unknown/VFSSetup/Initialize/Precheck/Scan/Finalize/Done/Failed/Aborted/Enqueued/Autocheck
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_stage VARCHAR(32);
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_progress INTEGER;
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_has_errors BOOLEAN;
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_error TEXT;
ALTER TABLE ptai_scans ADD COLUMN IF NOT EXISTS ptai_polled_at TIMESTAMPTZ;

COMMIT;
