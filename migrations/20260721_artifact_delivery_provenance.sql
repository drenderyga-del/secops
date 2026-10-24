CREATE TABLE IF NOT EXISTS artifacts (
    id SERIAL PRIMARY KEY,
    execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    source VARCHAR(64) NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'created',
    uri TEXT NOT NULL,
    digest_sha256 VARCHAR(64) NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    schema_version VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_artifact_execution_source_external UNIQUE (execution_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS ix_artifacts_execution ON artifacts (execution_id, created_at);

CREATE TABLE IF NOT EXISTS deliveries (
    id SERIAL PRIMARY KEY,
    execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    source VARCHAR(64) NOT NULL,
    destination VARCHAR(255) NOT NULL,
    attempt_key VARCHAR(255) NOT NULL DEFAULT 'initial',
    external_id VARCHAR(255),
    status VARCHAR(32) NOT NULL CHECK (status IN ('requested','running','confirmed','failed','skipped')),
    failure_category VARCHAR(32),
    failure_reason VARCHAR(64),
    error_text TEXT,
    attempted_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_delivery_artifact_attempt UNIQUE (artifact_id, destination, attempt_key)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_execution ON deliveries (execution_id, created_at);
CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries (status);

CREATE TABLE IF NOT EXISTS artifact_provenance (
    id SERIAL PRIMARY KEY,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    execution_step_id INTEGER REFERENCES execution_steps(id) ON DELETE SET NULL,
    source_revision VARCHAR(255),
    source_ref VARCHAR(255),
    observed_at TIMESTAMPTZ NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_artifact_provenance_artifact UNIQUE (artifact_id)
);
CREATE INDEX IF NOT EXISTS ix_artifact_provenance_execution ON artifact_provenance (execution_id, observed_at);
