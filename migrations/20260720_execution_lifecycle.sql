CREATE TABLE IF NOT EXISTS executions (
    id SERIAL PRIMARY KEY,
    repository_id INTEGER REFERENCES repositories(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    source VARCHAR(64) NOT NULL,
    correlation_key VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    failure_category VARCHAR(32),
    failure_reason VARCHAR(64),
    requested_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ,
    CONSTRAINT uq_execution_source_correlation UNIQUE (source, correlation_key)
);
CREATE INDEX IF NOT EXISTS ix_executions_repository ON executions (repository_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_executions_status ON executions (status);

CREATE TABLE IF NOT EXISTS execution_steps (
    id SERIAL PRIMARY KEY,
    execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    source VARCHAR(64) NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    name VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    failure_category VARCHAR(32),
    failure_reason VARCHAR(64),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ,
    CONSTRAINT uq_execution_step_observation UNIQUE (execution_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS ix_execution_steps_execution ON execution_steps (execution_id, created_at);
