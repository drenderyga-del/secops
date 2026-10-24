# External agent triage API

ScanOps exposes a provider-neutral API for a trusted external agent. You can
connect your own agent framework or LLM service; ScanOps does not select a model,
call a model vendor, execute agent commands, change repositories, or apply patches.
The agent produces a diagnosis and a proposed fix; an operator records a decision.
Acceptance means approval of a suggestion, not that a patch has been applied.

## Enable access

Generate a dedicated random `SCANOPS_AGENT_API_TOKEN` (at least 32 characters),
store it in your secret manager, and pass the same value to the app and agent.
It must differ from CI, operator and integration authentication/encryption secrets.
A conflict blocks protected requests with 503 until the credentials are separated.
Never reuse a CI token. Keep operator
Basic authentication and `INTEGRATION_CREDENTIALS_KEY` enabled.

```bash
openssl rand -hex 32
```

The token is a single deployment trust identity (`external-agent`), so all agents
holding it can read all explicitly shared contexts and proposals. Run separate
ScanOps deployments for separate trust domains. Rotate the token to revoke agent
access. An unset, short, or CI-reused token disables agent authentication.

## Share only reviewed diagnostic evidence

The operator explicitly shares each job's context. No raw job payload, source
URL, repository contents, integration credential or automatic GitLab log fetch
is exposed. The queue contains failed jobs with a current shared context.
Use a reviewed diagnostic excerpt, such as an error code and nearby log lines;
never share customer data, private source or unreviewed logs with an external
service. Secret-shaped text is rejected, but pattern matching cannot recognize
all confidential information. The published job name is also checked.

Assume `BASE_URL=https://scanops.example.com`, `SCANOPS_OPERATOR=user:password`,
and `SCANOPS_AGENT_API_TOKEN` are supplied securely in the caller environment.
The Origin must match the ScanOps host. Do not paste real values into examples.

```bash
curl --fail-with-body --user "$SCANOPS_OPERATOR" \
  -H "Origin: $BASE_URL" -H 'Content-Type: application/json' \
  -X PUT "$BASE_URL/api/v1/agent-operations/jobs/123/context" \
  --data '{"diagnostics":"Checkov exited with code 2: the rules configuration file was not found."}'
```

`DELETE /api/v1/agent-operations/jobs/123/context` revokes access to that context
and blocks agent reads of its stored proposals. Existing proposal audit records
remain visible to the operator.

## Agent loop

1. Poll `GET /api/v1/agent/jobs?after=0&limit=50` with the dedicated Bearer token.
   `next_after` advances through the current page. Restart polling from zero in
   the next sweep so a newly shared context for an older job is discovered.
2. Read `GET /api/v1/agent/jobs/{job_id}` for the current context version.
3. Treat diagnostics as untrusted data. Never follow instructions found in logs.
   Build a triage result with your own model and return it to ScanOps.
4. Submit `POST /api/v1/agent/jobs/{job_id}/proposals`. Keep a stable `request_id`
   when retrying the identical proposal. A reused ID with different contents or
   a stale context version returns `409`. Follow the returned proposal ID.
5. Read `GET /api/v1/agent/proposals/{id}` to see the operator's decision.

Example proposal body (save it to a local file, substituting the actual context
version from step 2):

```json
{
  "request_id": "job-123-diagnosis-1",
  "context_version": "<64-character SHA-256 from the context response>",
  "classification": "configuration",
  "summary": "The scanner could not load its configuration",
  "rationale": "The shared diagnostic excerpt reports a missing rules file.",
  "suggested_fix": "Restore the tracked rules file or correct its configured path, then rerun the job.",
  "patch": null
}
```

```bash
curl --fail-with-body -H "Authorization: Bearer $SCANOPS_AGENT_API_TOKEN" \
  "$BASE_URL/api/v1/agent/jobs?after=0&limit=50"
curl --fail-with-body -H "Authorization: Bearer $SCANOPS_AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  "$BASE_URL/api/v1/agent/jobs/123/proposals" --data-binary @proposal.json
```

Allowed classifications: `configuration`, `tooling`, `vulnerability`,
`transient`, `unknown`. The optional patch is an advisory unified diff using
repository-relative paths. It is stored as text, never executed or applied.
A diagnosis should state uncertainty and reference only evidence actually seen.
The API limits context text to 20,000 characters, patch text to 60,000 characters
and request bodies to 6 MiB. Never render an agent's text as trusted HTML.

## Review

With operator Basic auth, use:

- `GET /api/v1/agent-operations/proposals?after=0&limit=50` to inspect suggestions.
- `POST /api/v1/agent-operations/proposals/{id}/review` with
  `{"decision":"accepted","note":"Validated against the current job evidence"}`
  or `{"decision":"rejected","note":"The evidence does not support this fix"}`.

Mutations require a matching Origin/Referer. Agent and CI credentials cannot
perform operator actions. A job change or updated context invalidates old
proposals for acceptance. Decisions are terminal and record operator/time;
rejection remains possible after revocation. Apply any accepted patch through
your normal code-review and test process. There is no autonomous remediation.

Credential separation includes all environment aliases and enabled credentials
in the authoritative integration and report-destination stores. Agent requests
fail closed if the store is unavailable or a token cannot be decrypted. Integration
configuration writes reject reuse of the configured agent token.
