ALTER TABLE executions ADD COLUMN IF NOT EXISTS source_scope VARCHAR(255) NOT NULL DEFAULT 'default';
ALTER TABLE executions DROP CONSTRAINT IF EXISTS uq_execution_source_correlation;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_execution_source_scope_correlation'
    ) THEN
        ALTER TABLE executions ADD CONSTRAINT uq_execution_source_scope_correlation
            UNIQUE (source, source_scope, correlation_key);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS execution_events (
    id SERIAL PRIMARY KEY,
    execution_id INTEGER REFERENCES executions(id) ON DELETE SET NULL,
    schema_version VARCHAR(16) NOT NULL,
    event_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL,
    source_scope VARCHAR(255) NOT NULL DEFAULT 'default',
    correlation_key VARCHAR(255) NOT NULL,
    reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_sha256 VARCHAR(64) NOT NULL,
    payload_json TEXT NOT NULL,
    CONSTRAINT uq_execution_event_source_scope_id UNIQUE (source, source_scope, event_id)
);
CREATE INDEX IF NOT EXISTS ix_execution_events_execution ON execution_events (execution_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_execution_events_source_correlation ON execution_events (source, source_scope, correlation_key, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_execution_events_type ON execution_events (event_type);
