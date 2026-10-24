-- IaC upload targets an existing ASOC repository (by slug), not a product.
-- IF NOT EXISTS keeps this idempotent with Base.metadata.create_all on fresh DBs.
ALTER TABLE iac_scan_repositories ADD COLUMN IF NOT EXISTS asoc_repository_slug VARCHAR;
ALTER TABLE iac_scan_repositories ADD COLUMN IF NOT EXISTS asoc_repository_id VARCHAR;
