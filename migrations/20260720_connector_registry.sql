CREATE TABLE IF NOT EXISTS connectors (
    id SERIAL PRIMARY KEY,
    key VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    connector_type VARCHAR(64) NOT NULL,
    endpoint VARCHAR(1024),
    credential_ref VARCHAR(256),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    health_status VARCHAR(24) NOT NULL DEFAULT 'unknown',
    health_reason TEXT,
    last_seen_at TIMESTAMPTZ,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_connectors_type ON connectors (connector_type);
CREATE INDEX IF NOT EXISTS ix_connectors_health ON connectors (health_status);

CREATE TABLE IF NOT EXISTS scanner_instances (
    id SERIAL PRIMARY KEY,
    connector_id INTEGER NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    instance_key VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    instance_type VARCHAR(32) NOT NULL DEFAULT 'scanner',
    endpoint VARCHAR(1024),
    version VARCHAR(128),
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    health_status VARCHAR(24) NOT NULL DEFAULT 'unknown',
    health_reason TEXT,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_scanner_instances_connector ON scanner_instances (connector_id);
CREATE INDEX IF NOT EXISTS ix_scanner_instances_health ON scanner_instances (health_status);
