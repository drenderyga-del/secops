CREATE TABLE IF NOT EXISTS integration_configs (
    connector VARCHAR(32) PRIMARY KEY,
    values_json TEXT NOT NULL DEFAULT '{}',
    secrets_encrypted TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tested_at TIMESTAMPTZ,
    test_status VARCHAR(24),
    test_message TEXT
);

CREATE TABLE IF NOT EXISTS integration_config_events (
    id SERIAL PRIMARY KEY,
    connector VARCHAR(32) NOT NULL,
    action VARCHAR(32) NOT NULL,
    actor VARCHAR(128),
    changed_fields_json TEXT NOT NULL DEFAULT '[]',
    success BOOLEAN NOT NULL DEFAULT TRUE,
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_integration_config_events_connector
    ON integration_config_events (connector);

CREATE INDEX IF NOT EXISTS ix_integration_config_events_created_at
    ON integration_config_events (created_at DESC);
