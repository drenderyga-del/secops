-- Defensive replacement for the old in-code _run_column_migrations() from
-- app/main.py. All of these columns exist in the SQLAlchemy models (so fresh
-- DBs get them via create_all) and on production (the old code ran for ages).
-- Kept as idempotent inserts purely as insurance for DBs restored from old
-- backups.

ALTER TABLE config_scan_runs ADD COLUMN IF NOT EXISTS report_json_path VARCHAR;
ALTER TABLE config_scan_runs ADD COLUMN IF NOT EXISTS report_html_path VARCHAR;
ALTER TABLE config_scan_runs ADD COLUMN IF NOT EXISTS scope_path VARCHAR;
ALTER TABLE job_reports ADD COLUMN IF NOT EXISTS duration_seconds FLOAT;
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS duration_seconds FLOAT;
ALTER TABLE active_pipelines ADD COLUMN IF NOT EXISTS web_url VARCHAR;
ALTER TABLE active_pipelines ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE active_pipelines ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE active_pipelines ADD COLUMN IF NOT EXISTS duration_seconds FLOAT;
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS last_long_running_alert_at TIMESTAMPTZ;
ALTER TABLE products ADD COLUMN IF NOT EXISTS asoc_product_slug VARCHAR;
ALTER TABLE products ADD COLUMN IF NOT EXISTS asoc_product_id VARCHAR;
ALTER TABLE products ADD COLUMN IF NOT EXISTS asoc_sync_status VARCHAR(32);
