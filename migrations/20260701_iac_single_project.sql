-- Single-repository IaC rescan: mark a run that targets one GitLab project.
-- IF NOT EXISTS keeps this idempotent with Base.metadata.create_all on fresh DBs.
ALTER TABLE iac_scan_runs ADD COLUMN IF NOT EXISTS single_project_id INTEGER;
