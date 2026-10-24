CREATE TABLE IF NOT EXISTS integration_instance_test_states (
    connector VARCHAR(32) NOT NULL,
    instance_id VARCHAR(64) NOT NULL,
    tested_at TIMESTAMPTZ NOT NULL,
    test_status VARCHAR(24) NOT NULL,
    test_message TEXT,
    last_success_at TIMESTAMPTZ,
    last_success_message TEXT,
    PRIMARY KEY (connector, instance_id)
);

CREATE INDEX IF NOT EXISTS ix_integration_instance_test_states_status
    ON integration_instance_test_states (connector, test_status);
