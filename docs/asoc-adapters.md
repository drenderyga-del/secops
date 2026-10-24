# ASOC report destinations

The versioned API supports multiple independent destinations with encrypted
credentials. Each destination selects an adapter, an API base URL and a target
within that provider. There is no deployment-specific default server or token.
Discover the contract through `GET /api/v1/asoc/providers` or `/docs`.

| Adapter | Report format | Target settings | Native operation |
| --- | --- | --- | --- |
| Hexway ASOC / Vampy | SARIF 2.1.0 | `repository`, `branch`, `vcs_url` | `POST /scan_uploads/` |
| DefectDojo | SARIF 2.1.0 | `engagement_id`, `test_title` | `POST /reimport-scan/` |
| OWASP Dependency-Track | CycloneDX JSON SBOM | `project_id` UUID | `POST /bom` |

Dependency-Track is an SBOM and dependency-risk platform, not a generic SARIF
findings importer. A capability mismatch is rejected without contacting the
provider. SARIF reports must identify exactly one scanner; keep a distinct
DefectDojo `test_title` for each scanner, repository and branch so their histories
do not overlap. DefectDojo requires an existing engagement; it never creates
context automatically, closes old findings, or reactivates closed findings.
Dependency-Track requires an existing project. There is no automatic product
creation, inventory synchronization, or vulnerability triage across providers.

The existing **ASOC / Vampy** integration remains responsible for ASOC product
cards, PT AI orchestration and the existing IaC worker. These flows still use
`HEXWAY_*` settings. The new destination API is an explicit report-delivery
interface for CI and external integrations; creating a destination does not
silently reroute the existing worker or replace the product registry.

## Configure a destination

Operator Basic auth and a matching Origin are required to create, replace,
list or test destinations. The agent credential cannot manage destinations.
`INTEGRATION_CREDENTIALS_KEY` is mandatory for destination creation and decryption,
must differ from authentication secrets, and must remain stable. The legacy CI
key fallback does not apply to the new destination store.
The legacy credential-migration command covers the original integration store,
not these new destinations. Before rotating the dedicated encryption key, keep
provider tokens in your secret manager, disable deliveries, and replace each
destination's complete configuration/token using PUT under the new key. Re-enable
and check every destination afterward. Never claim a legacy-store migration has
rotated the destination store.
All provider URLs use HTTPS, TLS verification stays enabled and HTTP redirects
are never followed. Install private CAs in the deployment trust store if needed.

Example DefectDojo configuration (save locally, replacing the token securely):

```json
{
  "name": "Engineering findings",
  "provider": "defectdojo",
  "api_base_url": "https://defectdojo.example.com/api/v2",
  "token": "<provider API token>",
  "settings": {"engagement_id": "12", "test_title": "checkov-example-repo-main"},
  "enabled": true
}
```

```bash
curl --fail-with-body --user "$SCANOPS_OPERATOR" \
  -H "Origin: $BASE_URL" -H 'Content-Type: application/json' \
  "$BASE_URL/api/v1/asoc/destinations" --data-binary @destination.json
curl --fail-with-body --user "$SCANOPS_OPERATOR" \
  -H "Origin: $BASE_URL" -X POST \
  "$BASE_URL/api/v1/asoc/destinations/1/check"
```

For Vampy use `api_base_url=https://asoc.example.com/api` with settings such as
`{"repository":"example/repo","branch":"main","vcs_url":"https://gitlab.example.com/example/repo"}`.
For Dependency-Track use `api_base_url=https://dependencytrack.example.com/api/v1`
and `{"project_id":"<existing project UUID>"}`. Each supports a separate token.
`PUT /api/v1/asoc/destinations/{id}` replaces configuration, including the token;
set `enabled=false` to disable it. Responses never include credentials.

The connection check reads the configured engagement/project, or Vampy's
products endpoint. It verifies authenticated read access; import permissions
are verified only by an actual report submission.

## Deliver and inspect

Use the CI Bearer credential (`PIPE_BEARER_TOKEN`) to submit a JSON envelope:

```json
{
  "request_id": "pipeline-123-checkov-report-1",
  "report_format": "sarif",
  "report": {
    "version": "2.1.0",
    "runs": [{"tool":{"driver":{"name":"Checkov"}},"results":[]}]
  }
}
```

```bash
curl --fail-with-body -H "Authorization: Bearer $PIPE_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  "$BASE_URL/api/v1/asoc/destinations/1/reports" --data-binary @delivery.json
```

For Dependency-Track set `report_format=cyclonedx` and provide the generated
CycloneDX JSON object in `report`. Limit the serialized report to 5 MiB; the
HTTP envelope is bounded to 6 MiB. Use the same request ID for an identical
retry. A successful replay returns the existing receipt without resending.
An ID reused for different content or another destination returns `409`.

`202 accepted` confirms provider acceptance of the upload, **not** completed
processing or availability of findings. `processing_verified` is always false.
Every configuration replacement creates a new immutable generation. Replays after
a configuration change return 409. Each attempt stores that generation and a
non-secret snapshot of its provider, URL and target settings, so an operator can
recover an ambiguous send even if the destination has since changed.
The journal records the destination, provider, format, SHA-256, status and remote
reference, without persisting the submitted report or upstream error text.
Operators can inspect `GET /api/v1/asoc/deliveries?after=0&limit=50`.

A timeout, mutating HTTP 5xx response or invalid success receipt is `unknown`; an interrupted process may
leave `pending`. Check the provider before creating another request ID, because
the remote side may have accepted the report. There is no automatic retry or
cross-provider exactly-once guarantee. Remote 3xx/4xx/5xx responses fail closed
with the HTTP status only. Provider response bodies and transport exception
messages are not returned to callers or stored in the journal.

The adapters follow the official [DefectDojo API documentation](https://docs.defectdojo.com/automation/api/api-v2-docs/)
and [Dependency-Track CI/CD documentation](https://docs.dependencytrack.org/usage/cicd/).
Contract tests use mocked provider responses. Live provider compatibility must
be verified against the specific deployed version using the connection check
and a synthetic report; no access to live vendor instances is assumed.

Provider credentials must differ from ScanOps CI, operator, agent and encryption
secrets. Credential-shaped API host/path segments are rejected before persistence.
