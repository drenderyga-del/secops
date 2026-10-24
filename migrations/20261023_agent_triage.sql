CREATE TABLE IF NOT EXISTS agent_job_contexts (
    job_report_id INTEGER PRIMARY KEY REFERENCES job_reports(id) ON DELETE CASCADE,
    context_text TEXT NOT NULL,
    version VARCHAR(64) NOT NULL,
    shared_by VARCHAR(128) NOT NULL,
    shared_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_triage_proposals (
    id SERIAL PRIMARY KEY,
    job_report_id INTEGER NOT NULL REFERENCES job_reports(id) ON DELETE CASCADE,
    agent_id VARCHAR(80) NOT NULL,
    request_id VARCHAR(80) NOT NULL,
    context_version VARCHAR(64) NOT NULL,
    payload_json TEXT NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by VARCHAR(128),
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    CONSTRAINT uq_agent_triage_request UNIQUE (agent_id, request_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_triage_proposals_job_report_id ON agent_triage_proposals(job_report_id);
