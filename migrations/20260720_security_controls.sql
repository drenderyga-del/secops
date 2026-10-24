-- Shared, scanner-neutral expected-control vocabulary for Internal coverage.
CREATE TABLE IF NOT EXISTS security_controls (
    id SERIAL PRIMARY KEY,
    control_key VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(64),
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS repository_control_bindings (
    id SERIAL PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    security_control_id INTEGER NOT NULL REFERENCES security_controls(id) ON DELETE CASCADE,
    scope VARCHAR(255) NOT NULL DEFAULT '*',
    status VARCHAR(32) NOT NULL DEFAULT 'missing',
    source VARCHAR(64),
    evidence TEXT,
    reason TEXT,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ,
    CONSTRAINT uq_repository_control_scope UNIQUE (repository_id, security_control_id, scope),
    CONSTRAINT ck_repository_control_status CHECK (status IN ('configured', 'missing', 'unknown', 'excluded'))
);

-- ``Base.metadata.create_all`` runs before this migration on a fresh app
-- startup. Keep the constraint when that earlier create made the table, and
-- retrofit it for any database that already has the table without the check.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_repository_control_status'
          AND conrelid = 'repository_control_bindings'::regclass
    ) THEN
        ALTER TABLE repository_control_bindings
            ADD CONSTRAINT ck_repository_control_status
            CHECK (status IN ('configured', 'missing', 'unknown', 'excluded'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_security_controls_control_key ON security_controls (control_key);
CREATE INDEX IF NOT EXISTS ix_repository_control_bindings_repository_id ON repository_control_bindings (repository_id);
CREATE INDEX IF NOT EXISTS ix_repository_control_bindings_security_control_id ON repository_control_bindings (security_control_id);
