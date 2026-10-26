# Extended operator reference

Start with the [setup guide](setup.md), [execution-event API](execution-event-ingestion-v1.md),
[ASOC adapters](asoc-adapters.md) and [agent API](agent-api.md). This reference
covers architecture, connector recovery and deployment operations in more detail.

## Architecture

- Backend: FastAPI, Python 3.12.
- Database: PostgreSQL 16 and SQLAlchemy 2.x.
- Frontend: React/Vite for canonical Signal Control sections; Jinja2, HTML,
  CSS and JavaScript for detail/operator workbenches in the same product shell.
  `/classic...` routes are compatibility redirects only.
- Infrastructure: Docker and Docker Compose.
- Authentication: operator HTTP Basic (`UI_AUTH_USERNAME`/`UI_AUTH_PASSWORD`)
  for the UI and ordinary protected reads; `PIPE_BEARER_TOKEN` for CI. Versioned
  agent/report APIs have the separate authentication contracts linked above.
  `/api/health` is open for container healthchecks.

Without `UI_AUTH_*`, ordinary UI authentication is disabled with a startup
warning. Manual ASOC synchronization remains closed without an operator account:
it requires same-origin operator Basic auth, and CI bearer access cannot invoke
it. This manual path alone can confirm bulk card removal. Scheduled product sync
in `pipeline_worker` is independent of UI auth while ASOC is connected. It applies
new/changed cards but never removes cards: any snapshot containing removals waits
for manual synchronization. Set `ASOC_PRODUCT_SYNC_INTERVAL_MINUTES=0` to disable
scheduled product sync.

### Services

One image runs with different commands:

| Service | Command | Responsibility |
| --- | --- | --- |
| `app` | uvicorn, the default image command | UI/API, startup migrations and config-scan polling. |
| `pipeline_worker` | `python -m app.workers.pipeline` | GitLab polling, long-running-job alerts, ASOC product sync and retention cleanup. |
| `iac_worker` | `python -m app.iac_worker` | Checkov/IaC scanning and ASOC uploads. |

Workers write heartbeats to `worker_heartbeats`, exposed through `/api/health`,
and `/tmp/secpipe-heartbeat` for the Docker healthcheck.

### Database migrations

`Base.metadata.create_all` creates new tables. Ordered SQL files in `migrations/`
(`YYYYMMDD_slug.sql`) update existing tables at app startup; `schema_migrations`
records applied changes. Files older than the baseline in
`app/migrations_runner.py` are marked applied without execution.

### Audit digest

`AUDIT_DIGEST_ENABLED=true` by default. Every `AUDIT_DIGEST_INTERVAL_DAYS`
(default 7), the pipeline worker sends Mattermost a digest of problematic
repositories: missing security jobs, broken pipelines or inaccessible projects.
The digest groups statuses and includes links, including `/coverage`. The last
send marker is stored in `app_state`, preventing duplicate sends on restart.

### ASOC enrichment: KEV and EPSS

With `ASOC_ENRICH_ENABLED=true`, the pipeline worker fetches CISA KEV and FIRST
EPSS daily. It queries Vampy `/scan_issues/filter/` using `cveIDs` and
`findingState=DETECTED`, then comments on matching open issues with KEV
exploitation or EPSS evidence. `(issue, kind)` pairs in `asoc_enriched_issues`
prevent duplicate comments. `ASOC_ENRICH_RAISE_SEVERITY=true` additionally raises
KEV issues to CRITICAL; this is disabled by default, leaving comments only.

If cisa.gov or epss.cyentia.com is unavailable, configure local mirrors with
`KEV_FEED_URL=/path/kev.json` and `EPSS_FEED_URL=/path/epss.csv.gz`.
`file://` URLs are also supported.

### Retention

`SECPIPE_RETENTION_DAYS` defaults to 365 in Compose; `0` disables cleanup.
Daily cleanup removes old `job_reports`, terminal `active_pipelines`, and old
config-scan, IaC and audit runs. The latest run for each product/scope is always
retained.

Read [SECURITY.md](../SECURITY.md) before publication. Examples use demonstration
addresses and require your own deployment credentials.

## Configuration

### Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL connection string. | `postgresql://postgres:password@localhost:5432/security_dashboard` |
| `PIPE_BEARER_TOKEN` | CI API token. | Required. |
| `APP_PORT` | FastAPI port. | `8000` |
| `GITLAB_API_URL` | Default GitLab REST API. | `https://gitlab.com/api/v4` |
| `GITLAB_PAT` | Poller PAT/project token; connection tests require authentication and access to at least one project. | Empty disables polling. |
| `GITLAB_INSTANCES` | JSON array of `{host, api_url, token}` for multiple instances. | Optional. |
| `PIPELINE_POLL_INTERVAL_SECONDS` | Polling interval. | `300` |
| `PIPELINE_POLL_BATCH_SIZE` | Pipelines per cycle. | `10` |
| `PIPELINE_MAX_AGE_SECONDS` | Age before an active pipeline becomes stale. | `86400` |
| `INTEGRATION_TEST_TIMEOUT_SECONDS` | Operator-triggered connector test timeout. | `10` |
| `MATTERMOST_WEBHOOK_URL` | Incoming alert webhook; also configurable through Integrations. | Empty. |
| `ALERT_SMTP_HOST` / `ALERT_EMAIL_TO` | Optional SMTP channel; both must be configured together. | Empty. |
| `SECPIPE_JOB_LONG_ALERT_*` | Long-running-job thresholds, intervals and filters. | See `.env.example`. |
| `PTAI_SCAN_STATUS_*` | Terminal/hung PT AI scan polling and alerts. | See `.env.example`. |
| `PTAI_QUEUE_ALERT_*` | PT AI queue thresholds and intervals. | See `.env.example`. |
| `HEALTH_EXPECTED_WORKERS` | Heartbeats required for healthy operational status. | `pipeline_worker,iac_worker` |

`scripts/preflight_internal.py` checks production secrets and alert configuration
without network requests. Because Mattermost can be managed in the UI, missing
environment-backed webhook/SMTP configuration produces a warning rather than a
false hard failure. After startup, require `operational_status=healthy` and test
the enabled Mattermost connector through Integrations.

`PRODUCT_NESTED_NAMESPACES` lists top-level namespaces whose second path group
identifies a product. The default list is empty, so the first path component is
the product. Configure this explicitly for your GitLab layout. Existing deployments
using two-level attribution must configure it before upgrading to preserve
product identities.

### Report delivery and external AI agents

The versioned report API supports multiple explicit destinations: Hexway
ASOC/Vampy, DefectDojo (SARIF) and Dependency-Track (CycloneDX SBOM).
[ASOC adapters](asoc-adapters.md) describes capabilities, connection checks and
CI delivery examples. Existing Vampy product-registry, PT AI and IaC flows remain
separate; configuring a report destination does not switch them automatically.

External agents can read explicitly shared failed-job contexts and submit triage
and advisory patches for operator acceptance/rejection. They require a dedicated
`SCANOPS_AGENT_API_TOKEN`, not the CI token. ScanOps does not automatically send
data to an LLM. See the [agent API](agent-api.md) for the complete workflow.

### UI-managed integrations

Configure GitLab, ASOC/Vampy, PT AI Engine and Mattermost at `/integrations`.
Saved URLs and tokens are read from PostgreSQL, so integration-specific secrets
need not be supplied to every container through `.env`. Environment settings
remain a fallback until a connector is saved or explicitly disabled in the UI.

Add each GitLab connection with its own name, REST API URL ending in `/api/v4`,
and token. `GITLAB_INSTANCES` remains a backward-compatible environment format;
the UI does not require editing JSON. Save, enable and Test connection act on an
individual entry. Testing one connection does not alter another entry's health.
Latest-check and last-successful-check states are separate and contain no token
or encrypted payload.

Connection enabled controls credential availability; Pipeline polling controls
worker use. Multiple independently testable credentials can share a GitLab host,
but only one can poll that host: without product-to-instance binding, choosing
between two poller tokens would be ambiguous. Disabled entries do not enter the
runtime poller configuration.

Environment-backed GitLab settings remain read-only until the operator explicitly
confirms import into encrypted UI-managed storage. Viewing or testing Integrations
does not copy environment credentials into the database.

Test connection checks authentication and accessible-project discovery. It does
not run CI Lint on an arbitrary project or require permissions unrelated to
read connectivity. Config Guard runs CI Lint separately in a specific project;
availability depends on token role and project policy. A failure of that
project-scoped call does not invalidate already confirmed execution reads.

Secret fields are encrypted before persistence and never returned in HTML.
Production requires a dedicated `INTEGRATION_CREDENTIALS_KEY`, shared by the web
app and both workers. This lets CI bearer tokens rotate without breaking saved
integration credentials. Legacy local connector storage can fall back to
`PIPE_BEARER_TOKEN`; changing encryption material without migration makes stored
tokens unreadable. The key need not use Fernet format: generate a random value
with `openssl rand -hex 32`, retain it as a protected deployment secret, and do
not regenerate it on restart. If the old key is lost, re-enter every credential
field for the affected integration so unreadable ciphertext is replaced.
New report destinations require a dedicated key; their rotation procedure is
separate from the legacy-connector migration below. See [ASOC adapters](asoc-adapters.md).

For deterministic local success/failure/recovery checks without external requests,
use [qa/README.md](../qa/README.md). It provides GitLab, ASOC, PT AI and Mattermost
doubles and is separate from the browser release gate.

### Migrate legacy connector credentials

Before first configuring a dedicated key, retain the previous release/config,
keep the old `PIPE_BEARER_TOKEN` available only for migration, and take a verified
database backup. Production should use an approved snapshot with a tested restore.
For local Compose, create a mode-600 custom archive and verify that PostgreSQL can
read its catalogue:

```bash
docker compose stop app pipeline_worker iac_worker

umask 077
backup_dir=$(mktemp -d)
backup_file="$backup_dir/security_dashboard-before-credential-migration.dump"
docker compose exec -T postgres \
  pg_dump --username postgres --dbname security_dashboard \
  --format=custom --no-owner --no-privileges > "$backup_file"
chmod 600 "$backup_file"
test -s "$backup_file"
docker compose exec -T postgres \
  pg_restore --list < "$backup_file" >/dev/null
```

Do not continue if the archive is empty, has different permissions, or fails
`pg_restore --list`. Match the backup to the same
`postgres:5432/security_dashboard` target reported by the migration dry-run.

All processes that can save integration configuration must stay stopped through
the post-apply dry-run, preventing an old process from rewriting ciphertext under
the legacy key. The one-off container receives an explicit `DATABASE_URL` from
Compose. Dry-run output includes a redacted target, counts and connector names,
never secret values:

```bash
docker compose run --rm --no-deps app \
  python scripts/migrate_integration_credentials.py || {
    echo "Dry-run blocked; do not apply or start services." >&2
    exit 1
  }
```

If dry-run reports `blocked` or exit code 2, do not apply. Restore the correct
legacy key or use the documented rollback/reset procedure. After a successful
dry-run, inspect `database_target` and run the remaining commands as one block.
`set -e` prevents service startup after any failure:

```bash
set -e
# Copy database_target from the dry-run without changes.
docker compose run --rm --no-deps app \
  python scripts/migrate_integration_credentials.py \
  --apply --confirm-target postgres:5432/security_dashboard

# Do not start services until this dry-run reports:
# status=ready_to_apply, remaining_irrecoverable=0 and the expected database_target.
docker compose run --rm --no-deps app \
  python scripts/migrate_integration_credentials.py

docker compose up -d
```

After startup, open `/integrations` and Test connection for every enabled connector.
Checks must not expose stored secrets in HTML, logs or command output.

After successful apply, saved credentials are read with
`INTEGRATION_CREDENTIALS_KEY`, allowing rotation of `PIPE_BEARER_TOKEN`. If apply
or post-apply dry-run reports `blocked` or a nonzero status, do not start services:
recoverable rows may already have been committed even though irrecoverable rows
remain.

### Roll back a credential migration

Stop services, preserve the failed database for investigation, and restore the
verified pre-migration backup. Deploy the retained previous release/config that
still supports legacy `PIPE_BEARER_TOKEN` storage. Current Compose cannot start
without a dedicated `INTEGRATION_CREDENTIALS_KEY`; migration preflight rejects an
empty key or one equal to the legacy token. Before retrying, stop the old release,
configure a new dedicated key different from the old bearer, and repeat dry-run.

In local Compose, restore only after explicitly deciding to roll back and checking
the archive path and database target:

```bash
test -s "$backup_file"
docker compose stop app pipeline_worker iac_worker
docker compose exec -T postgres \
  pg_restore --clean --if-exists --single-transaction \
  --username postgres --dbname security_dashboard < "$backup_file"
```

Keep the backup and old bearer until post-apply dry-run and every connector check
have succeeded.

### Reset approved unreadable credentials

Reset is not rollback. It removes only approved unreadable credentials, which an
operator must then re-enter through the UI. It requires a mode-600 manifest from
a separate dry-run. The manifest binds the redacted database target, connector
names and SHA-256 of the current ciphertext. Any changed row or database target
blocks the entire reset:

```bash
recovery_dir=$(mktemp -d)
chmod 700 "$recovery_dir"

docker compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$recovery_dir:/recovery" app \
  python scripts/migrate_integration_credentials.py \
  --write-reset-manifest /recovery/reset.json

# Review irrecoverable connectors before the irreversible reset.
docker compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$recovery_dir:/recovery" app \
  python scripts/migrate_integration_credentials.py \
  --apply --confirm-target postgres:5432/security_dashboard \
  --reset-irrecoverable --reset-manifest /recovery/reset.json
```

Reset clears only rows matching the manifest, clears their previous health checks
and disables the affected connectors.

Integration changes require enabled UI authentication through
`UI_AUTH_USERNAME` and `UI_AUTH_PASSWORD`. Saves, disabling and connection tests
are recorded in `integration_config_events` without secret values.

### Unified execution lifecycle

GitLab pipelines/jobs, Config Scan, IaC and PT AI/ASOC write canonical states to
`executions` and `execution_steps`. Scanner-specific tables remain operational
sources; the shared lifecycle supplies one repository timeline across scanners.
Executions are idempotent by `(source, source_scope, correlation_key)` and steps
by `(execution_id, source, external_id)`. A terminal state cannot regress to
running or queued.

`GET /api/repositories/{repository_id}/executions` returns canonical status,
timestamps, safe diagnostic context and ordered steps, artifacts, producer
provenance and delivery attempts. A successful scanner exit alone does not prove
end-to-end delivery. IaC requires nonempty SARIF with SHA-256 and confirmed ASOC
delivery. `no_iac` is a successful not-applicable outcome without an artifact.
Skipped/failed upload fails the original execution. Manual re-upload creates a new
delivery attempt without rewriting the original terminal history.

External jobs and adapters write scanner-neutral telemetry through
`POST /api/v1/execution-events`, using source-scoped identity
`(source, source_scope, event_id)`, a correlation key, source `observed_at` and
server `received_at`. Exact replay returns the existing event; the same identity
with a different payload returns `409 Conflict`. Read event history through
`GET /api/v1/execution-events`. See the [event contract](execution-event-ingestion-v1.md).

### PT AI scan orchestration

PT AI scans run through Vampy/ASOC orchestration rather than the ScanOps pipeline
monitor. A GitLab `ptai_scan` job in your security pipeline calls Vampy
`POST /api/ext/v1/scan/` once with a profile (default `security-pipeline-ptai`),
repository slug and branch. Vampy clones the repository, runs the PT AI scanner
image, waits for its report and stores findings in the product.

The PT AI scanner image is built separately, for example as
`registry.example.com/scanners/ptai:<TAG>`. Vampy Profile Configuration must point
to that image.

ScanOps tracks asynchronous PT AI scans independently of the GitLab job. The
poller reads a completed `ptai_scan` trace and persists ASOC `pipeline_id` and
`task_id` in `ptai_scans`. The pipeline worker polls
`/ext/v1/scan/pipelines/{id}/status/`, falling back to the background task.
`/ptai-scans` shows the scanner's operational state; `/pipelines` shows GitLab
jobs, not actual PT AI progress inside ASOC. Repository, pipeline and task IDs
are displayed independently. A known repository keeps its link, while absent
pipeline/task IDs show `No pipeline/task ID`.

ASOC polling needs `HEXWAY_URL` and `HEXWAY_TOKEN`. Optional `PTAI_URL` and
`PTAI_TOKEN` add engine-side stage/progress/error data; ASOC status still updates
without them. With `project_id`, `branch_id` and `scan_result_id`, lists, details
and alerts link directly to
`/ui/projects/{project_id}/branches/{branch_id}/scans/{scan_result_id}`.
Override nonstandard deep links with `PTAI_RESULT_URL_TEMPLATE`. Alerts also link
to the GitLab job, ScanOps scan and ASOC repository. `SECPIPE_PUBLIC_URL` sets
the ScanOps URL; the ASOC UI URL is derived by removing the trailing `/api` from
`HEXWAY_URL`. The `/ext/v1/scan/` request identifier is not an ASOC repository scan
ID.

Manual IaC can use `IAC_HEXWAY_PRODUCT_ID`. If neither slug nor GitLab URL finds an
ASOC repository, ScanOps creates it in that dedicated ASOC product and uploads
SARIF. Existing ASOC repositories are not moved.

The IaC drill-down starts at `/iac/repositories`. Selecting a repository opens
its execution history. Selecting a run shows discover → Checkov → ASOC stages,
severity counts, execution metadata, errors and findings. Severity/text filters
work without reload; the selected active run is polled until terminal. Rescan
acts on the repository; re-upload and SARIF refer to the selected run.

The pipeline worker also monitors the PT AI engine queue every
`PTAI_QUEUE_ALERT_INTERVAL_SECONDS`. It counts projects whose stage matches
`PTAI_QUEUE_ALERT_STAGES` (default `Enqueued,Queued`) and alerts at
`PTAI_QUEUE_ALERT_THRESHOLD` (default 10). Resends are limited by
`PTAI_QUEUE_ALERT_RESEND_MINUTES`. Dropping below the threshold resets alert state,
so a later spike alerts immediately.

## Production deployment

The production Dockerfile uses public Node/Python/Alpine images and package
indexes. Private registries, trust bundles and deploy keys belong in deployment
infrastructure. Build arguments `NODE_BASE_IMAGE`, `PYTHON_BASE_IMAGE`,
`NPM_REGISTRY_URL` and `PIP_INDEX_URL` are configurable. `Dockerfile.local` is for
the demo and tests.

Build and push an image:

```bash
   export SECPIPE_IMAGE="registry.example.com/scanops/scanops:${CI_COMMIT_TAG}"
   docker build \
     --platform linux/amd64 \
     -t "${SECPIPE_IMAGE}" .
   docker push "${SECPIPE_IMAGE}"
   ```

Create `.env`; these are placeholders, not deployable secrets:

```env
   POSTGRES_PASSWORD=change-me
   PIPE_BEARER_TOKEN=super-secret-token
   INTEGRATION_CREDENTIALS_KEY=separate-stable-encryption-key
   UI_AUTH_USERNAME=scanops
   UI_AUTH_PASSWORD=change-me-too
   GITLAB_PAT=gitlab-pat-or-empty
   SECPIPE_IMAGE=registry.example.com/scanops/scanops:v1.3.8
   PIPELINE_POLL_INTERVAL_SECONDS=120
   PIPELINE_POLL_BATCH_SIZE=20
   PIPELINE_MAX_AGE_SECONDS=10800
   GITLAB_INSTANCES=[{"host":"gitlab.internal","api_url":"https://gitlab.internal/api/v4","token":"pat"}]
   ```

Check the release configuration:

```bash
   python3 scripts/preflight_internal.py --env-file .env
   ```

Preflight does not print secrets. It fails closed on missing authentication,
missing dedicated encryption material, nonunique/short secrets, mutable image
tags or disabled TLS verification. A temporary internal TLS exception requires
an explicit `--allow-insecure-tls` flag.

Start the stack:

```bash
   docker compose --env-file .env up -d
   ```

Check operational readiness:

```bash
   curl -fsS http://127.0.0.1:8081/api/health
   ```

HTTP 200 confirms web/database availability. Require `operational_status=healthy`;
`degraded_reasons` identifies missing/stale workers, delayed polling, disabled UI
authentication or missing credential-store key.

Services include PostgreSQL with `postgres_data` and a loopback-only database
port; app UI/API, migrations and Config Scan scheduling on host port 8081;
`pipeline_worker` for GitLab polling/alerts; and `iac_worker` for Checkov and SARIF
delivery. Config Scan JSON/HTML exports persist in `config_scan_reports` across
app replacement. ASOC/PT AI TLS verification is enabled by default; temporary
exceptions are limited to a trusted internal deployment.

GitLab Test connection checks `/version` and accessible-project discovery.
Project-scoped Config Guard CI Lint is separate from connection testing.

### Connector and ScannerInstance registry

`GET /api/registry` and Integrations show read-only operational snapshots:
logical connector, endpoint, credential reference, live scanner/worker instances,
versions/capabilities, health reason and last-seen time. Scanner fleet uses safe
connection state and separates latest run from seven-day workload. Endpoints and
credentials stay in Integrations. `IntegrationConfig` remains authoritative for
configuration/encrypted credentials; registry data contains only redacted
`credential_ref`, never tokens or encrypted payloads.

Health states are `healthy`, `degraded`, `stale`, `unknown` and `disabled`.
Disabled connectors are excluded from success rate; a worker heartbeat older than
30 minutes is stale. The registry observes runtime state and does not start,
stop or retry scans.

### Export to a private deployment repository

The public repository holds source, tests and documentation. The export wrapper
selects deployable files rather than requiring manual copying:

```bash
bash scripts/sync_internal_release.sh --remote origin \
  /absolute/path/to/internal/secpipe-monitor v1.2.0
```

The release flow fetches the exact Git tag, creates a temporary detached worktree
at that revision, and exports it into a clean deployment repository. Local changes
and develop state are not included:

```bash
bash scripts/sync_internal_release.sh \
  /absolute/path/to/internal/secpipe-monitor \
  v1.2.0
```

The historical default source remote is `personal-gitlab`. Pass your actual
remote explicitly; for the public GitHub checkout use `origin`:

```bash
bash scripts/sync_internal_release.sh --remote origin \
  /absolute/path/to/internal/secpipe-monitor v1.2.0
```

The wrapper does not commit or push the destination. Inspect its diff, then make
that deployment commit explicitly:

```bash
git add -A
git commit -m "release: ScanOps v1.2.0"
git push
```

The deployment repository owns its production CI. The public repository validates
source and does not publish production images. Exports include deployable source,
Dockerfile, Compose and migrations, while preserving destination `.git` and
`.gitlab-ci.yml`.

For a legacy destination without an export manifest, first verify a clean tracked
checkout and initialize the manifest explicitly:

```bash
bash scripts/sync_internal_release.sh --initialize-manifest --remote origin \
  /absolute/path/to/internal/secpipe-monitor v1.2.0
```

Initialization accepts only overlapping tracked paths and rejects untracked
source/destination files. Production CI and secrets remain destination-owned.
Tests, documentation, design previews, local environments and Git metadata are
not exported. The script does not use a global delete. `.scanops-export-manifest`
tracks source-owned export files; later exports delete only previously exported
source files no longer present. `.env`, `.gitlab-ci.yml`, `.scanops-release` and
internal-only files are preserved.

## Data model

| Table | Main fields |
| --- | --- |
| `products` | `id`, `name`, `path`, `gitlab_base_url` |
| `repositories` | `id`, `product_id`, `path`, `last_report_at`, `overall_status` |
| `job_reports` | `id`, `repository_id`, `pipeline_url`, `job_name`, `job_status`, `finished_at`, `ref` |

`job_report_details` stores the original JSONB payload for diagnosis.

## Interface

Navigation separates Operations (Overview and Executions), Security posture
(Coverage and Targets), and Infrastructure (Scanner fleet and Integrations).
Execution sections open Jobs, Pipelines, Config Guard, IaC and PT AI at canonical
URLs. Short pages do not repeat navigation with decorative Jump to links. Long
pages use one sticky On this page block with scroll tracking, keyboard links and
persistent hash URLs. View switches and filters retain distinct visual patterns.

Go to… opens by click or Ctrl/Cmd+K, filters canonical screens and supports
arrow keys, Enter and Escape.

- Overview (`/`) shows terminal security-job success rate over the selected
  period with explicit numerator/denominator, terminal pipeline failures,
  observed steps, normalized artifacts, execution flow, all-time attention,
  throughput and median latency. Live, Stale and no-data states are explicit.
- Jobs (`/logs`) is the execution journal; `status=failed` is the recovery filter.
  Legacy `/failed` redirects there while preserving the query.
- Coverage (`/coverage`) shows expected controls and gaps; `/audit` remains a
  compatibility redirect.
- Targets (`/targets`) browses Product and Repository levels. Product aggregate
  is `/targets/products/{id}`. Target 360 at `/targets/repositories/{id}` shows
  controls, freshness, observed versions, execution timelines, artifacts,
  deliveries and source-event evidence. `/products`, `/repositories` and
  `/repos/{id}` redirect with their query strings; `/repos/{id}/jobs` remains
  the detailed Operations journal.
- Open Target 360 opens canonical repository details; View job logs opens
  `/repos/{id}/jobs`. ASOC monitoring links require an actual product mapping.
- Scanner fleet (`/scanners`) lists live runtimes. Adapter runtime details opens
  `/scanners/operator`, separating Connection, Latest run and seven-day Workload.
  GitLab Last seen is the latest persisted job observation from a successful
  poll, not the latest failed poll or connection test. Only a successful latest
  connection test marks a source Connected. Untested configuration remains
  Configured · untested and is excluded from Adapters connected. Health reasons
  identify the source, with links to Integrations rather than duplicated secrets.
- Products (`/targets?level=product`) is the quarterly ASOC registry described in
  [Signal Control](signal-control.md). Its statuses include Synced, Partially
  synced, Conflict, Group not in ScanOps, No GitLab link, Waiting for sync and
  Removed from ASOC. Group coverage counts scanned/not-scanned/no-pipeline
  repositories over 30 days, with 30-day pipeline success. Audience comes from
  `ScanOps-Scope: internal|external` in ASOC Description. Conflicting unmatched
  groups are listed with ASOC card conflict. Product/card, quarter, waiting and
  removal behavior follow the same registry contract, including manual approval
  of removals and `PRODUCT_SYNC_GRACE_DAYS` (default 3). Scheduled sync defaults
  to 60 minutes; `0` disables it.
- Integrations (`/integrations`) shows connector settings immediately after the
  selected registry item, including endpoint-copy access.

Wide Target 360, Pipelines and PT AI tables retain readable desktop columns
(from 1440 px) inside labelled focusable scroll regions; use left/right arrows
when focused. Long diagnostic/evidence fields initially show two-line excerpts.
Expand full … reveals the value; Copy … copies it without truncation.

## Storage and retention

`job_reports` contains metadata only: repository, job name/status, ref, pipeline
and timestamps. `job_report_details` retains source JSON; remove old details with:

```python
  from app.database import DatabaseService
  deleted = DatabaseService(db).purge_job_details_older_than(days=30)
  print(f"Removed {deleted} outdated payloads")
  ```

A time-partition example using `finished_at`:

```sql
  CREATE TABLE job_reports_2024_11
    PARTITION OF job_reports
    FOR VALUES FROM ('2024-11-01') TO ('2024-12-01');
  ```

See `migrations/20241101_split_job_details.sql` for the details split.
The UI uses a dark theme, responsive layout, status badges, filter autocomplete,
tooltips and small animations.

`ASOC_GITLAB_HOST_PREFIXES` optionally maps GitLab hostnames to existing ASOC slug
prefixes as JSON. The default prefix is the full hostname. Existing deployments
must configure compatible prefixes before upgrading; private host mappings remain
outside public source.
