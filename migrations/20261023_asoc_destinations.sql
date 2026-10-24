CREATE TABLE IF NOT EXISTS asoc_destinations (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    api_base_url VARCHAR(2048) NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    secrets_encrypted TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    updated_by VARCHAR(128) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS asoc_report_deliveries (
    id SERIAL PRIMARY KEY,
    request_id VARCHAR(80) NOT NULL UNIQUE,
    destination_id INTEGER NOT NULL REFERENCES asoc_destinations(id) ON DELETE RESTRICT,
    provider VARCHAR(32) NOT NULL,
    report_format VARCHAR(32) NOT NULL,
    digest_sha256 VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL,
    remote_reference VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_asoc_report_deliveries_destination_id ON asoc_report_deliveries(destination_id);
