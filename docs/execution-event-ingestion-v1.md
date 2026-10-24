# Execution event ingestion v1

Release: `INT-1 Execution Core`

`POST /api/v1/execution-events` is the only public write path for external
scanner/job lifecycle telemetry. It stores an immutable event first and then
projects it into the shared Execution, ExecutionStep, Artifact and Delivery
contracts in the same database transaction.

## Envelope

```json
{
  "schema_version": "1.0",
  "event_id": "pipeline-100-running",
  "event_type": "execution.transition",
  "source": "gitlab_adapter",
  "source_scope": "gitlab.example/platform",
  "correlation_key": "pipeline:42:100",
  "observed_at": "2026-07-21T01:00:00Z",
  "repository_id": 7,
  "product_id": 3,
  "reason": "pipeline accepted by runner",
  "data": {"status": "running"}
}
```

- Authenticate with `Authorization: Bearer $PIPE_BEARER_TOKEN`.
- `observed_at` must include a timezone. ScanOps also stores `received_at`.
- `(source, source_scope, event_id)` is the immutable idempotency identity.
- An exact replay returns HTTP 200 and `replayed: true`; first acceptance
  returns HTTP 201. A different payload under the same identity returns 409.
- Child events require a prior `execution.transition` for the same
  `(source, source_scope, correlation_key)`.
- Secret-bearing keys are rejected recursively and must never be sent in data.

## Event types

`execution.transition` data:

```json
{"status":"failed","failure_category":"execution","failure_reason":"scanner_failed"}
```

`step.transition` data:

```json
{"external_id":"job:501","name":"SAST","status":"succeeded"}
```

`artifact.created` data:

```json
{
  "external_id":"sarif:501",
  "kind":"sarif",
  "uri":"reports/501.sarif",
  "digest_sha256":"<64 hex characters>",
  "size_bytes":128,
  "schema_version":"2.1.0",
  "source_revision":"deadbeef",
  "source_ref":"main",
  "execution_step_external_id":"job:501"
}
```

`delivery.transition` data:

```json
{
  "artifact_external_id":"sarif:501",
  "destination":"asoc",
  "attempt_key":"pipeline:100",
  "status":"confirmed",
  "external_id":"asoc:report:501"
}
```

Canonical transition rules remain authoritative: a late event may enrich
history but cannot regress a terminal execution or step. Invalid projection
data returns 422 and neither event nor projection is committed.

## History and diagnosis

`GET /api/v1/execution-events` accepts optional `source`, `source_scope`,
`correlation_key`, `execution_id` and `limit` filters. Results are ordered by
source observation time and expose source, reason, observed time, received time
and sanitized event data. This is evidence/audit history; large logs and binary
artifacts remain outside the database.

Deleting or retaining a projected Execution sets the event's projection link
to null; it does not delete the immutable accepted envelope.
