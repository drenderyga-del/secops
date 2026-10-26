# ScanOps changelog

## Unreleased

- Replace the long README with a concise English overview, Docker demo
  quickstart and screenshots; move deployment details into dedicated guides.

- Bind report receipts to immutable destination versions and target snapshots;
  preserve unknown outcomes for ambiguous uploads and block credential reuse.
- Require a dedicated encryption key for new report destinations; make product
  namespace depth explicit deployment configuration with synthetic fixtures.


- Add explicit report destinations for Vampy, DefectDojo SARIF and
  Dependency-Track CycloneDX, with encrypted credentials, capability checks,
  idempotent delivery requests and a digest-based delivery journal.
- Add a dedicated external-agent API for operator-shared job context,
  advisory triage/patch proposals and version-checked operator decisions.

- Prepare a public source snapshot: public container and package defaults,
  synthetic configuration examples, no deployment-specific endpoints or CA paths.
- Make product-directory reference hosts an explicit deployment allowlist.
- Document private vulnerability reporting and reproducible secret scans.


## Unreleased

- Turned Products into the quarterly ASOC product registry: a product is
  connected when its ASOC card is created, each card shows its GitLab sync
  status, how long it has waited, repository scan coverage from the GitLab
  audit and 30-day pipeline success, and the `new`/`total` quarter slice is
  kept in the URL and exported to the same XLSX. Cards and GitLab groups
  open a product drill-down; groups without an ASOC card are listed apart.
- Retired the separate ASOC reporting page: `/reports`, `/reports/operator`
  and `/reports/export.xlsx` redirect to Products, translating `year` and
  `quarter` into the new period filter.
- Attribute GitLab-audited repositories to the product whose own group
  contains them before falling back to the top-level namespace, using one
  rule for Coverage (overview, operator workspace, product drill-down) and
  Products, so sibling product groups in one namespace no longer absorb each
  other's projects. Products lists only projects inside the product group;
  Coverage still attributes projects outside every group by namespace.
- Refresh ASOC product cards from the pipeline worker every
  `ASOC_PRODUCT_SYNC_INTERVAL_MINUTES` (default 60, `0` disables it) and show
  the last attempt; a scheduled run never archives a card (any removal waits
  for an operator's Synchronize ASOC) and stores failures only by reference.
  Defaults need no configuration; to change them, pass
  `ASOC_PRODUCT_SYNC_INTERVAL_MINUTES` and `PRODUCT_SYNC_GRACE_DAYS` to the app
  and pipeline worker in the deployment's own compose/env files.
- Record every sync that archives ASOC cards in `asoc_product_sync_events`
  (new `confirmed` column; migration `20260926_asoc_sync_event_confirmed.sql`),
  keep a card's stored creation date when a snapshot omits it or sends an
  implausible one (outside 2000–2099), and report how many undated cards a
  quarter view leaves out, on screen and in the XLSX summary.
- Count each repository once in quarterly totals when several cards link the
  same group, strip control characters from XLSX cells, and URL-encode ASOC
  IDs in product links.
- Count waiting days and overdue only for cards ScanOps has never linked (no
  link date, no linked group and not flagged `connected_before_tracking`), show
  cards linked before date tracking as "Unknown", list groups held in ASOC conflict under Products, and rename the
  operator page to Product workbench with explicit "Connected (ASOC card
  created)" and "Linked in ScanOps" dates.
- Group pages tell a card awaiting sync from cards in conflict (including
  several cards waiting for one group) and from links the sync cannot
  resolve; the last-attempt banner is keyed to when an attempt started, so a
  slower older attempt cannot hide a newer result.
- Group pages tell a card awaiting sync from a card in conflict or one that
  links only an unknown repository; the last-attempt banner is keyed to when
  an attempt started, so a slower older attempt cannot hide a newer result.
  `ASOC_PRODUCT_SYNC_INTERVAL_MINUTES` (default 60) and show the last
  attempt; a scheduled run never confirms a mass removal.
- Let deleting or merging a ScanOps product clear linked ASOC cards (the
  lifecycle write gate now allows only that `ON DELETE SET NULL` change), and
  record cards linked before date tracking so an unlink/relink never stamps a
  new connection date (migration
  `20260926_asoc_lifecycle_unlink_and_legacy_links.sql`).
- Keep ASOC Description links whose GitLab group or project names look like
  credential words (`secrets`, `oauth2`, `webhooks`…), trim GitLab `/-/`
  sub-routes to the identity, and recognize webhook-only GitLab hosts from
  existing ScanOps products; token-shaped values, `key=value` credentials and
  reserved GitLab routes are still dropped.
- Export releases with content checksums so a changed file with the same size
  and timestamp is never skipped.
- Preserve removed ASOC product cards and their first-connected dates for
  quarterly reporting; reject malformed snapshots and require explicit
  confirmation before mass or empty-catalog archival.
- Stop publishing target links for conflicted ASOC mappings, make partial and
  conflict states explicit in reports/exports, and serialize concurrent ASOC
  refreshes.
- Restrict ASOC catalog refresh to configured same-origin operator Basic auth,
  reject credential-bearing Description URLs, and keep upstream error payloads
  out of logs and browser URLs.
- Evaluate worker staleness only for configured expected workers, so disabled
  workers' retained heartbeats stay visible without degrading service health.
- Require a snapshot-bound operator confirmation before ASOC sync archives a
  substantial part (or all) of the active registry, and record confirmed
  removals without discarding quarterly history.
- Keep legacy ASOC descriptions to safe URLs and explicit scope markers,
  reject token/webhook paths, serialize app/worker schema setup, block unsafe
  ASOC writes from pre-lifecycle rollback binaries, protect lifecycle history
  from deletion, and export ASOC-controlled strings as spreadsheet text.
- Reuse the safe scope and Description-link projection in Product 360, and
  let the workerless disposable demo report healthy runtime status.

## 1.3.8 — 2026-08-31

- Made ASOC the canonical product registry: synchronized ASOC product cards
  with monitored ScanOps products and repositories by normalized GitLab URL,
  preserved ASOC-only and ScanOps-only records, exposed mapping state and
  delivery evidence, and added the repository-mapping migration and tests.
- Added a repeatable internal release/deployment flow with worktree-safe
  export, publish and deploy scripts plus release-flow contract tests.
- Unified execution, job-journal and Config Guard pagination/filter contracts,
  including URL-backed page state, shared controls, tablet touch targets and
  page-independent severity aggregates.
- Refined ScanOps branding and the integration/scanner-fleet radar with
  responsive geometry, live-refresh measurement, runtime-synchronized sweep
  animation, node pulses and deterministic browser coverage.
- Added regression coverage and operator documentation for the integrated ASOC,
  pagination and radar workflows.
- Hardened ASOC pagination against duplicate product cards and aligned the
  operator documentation with description-based URL mapping.
- Made internal exports remove only previously tracked source-owned files,
  while preserving destination-owned configuration and internal-only files;
  dry-run release validation now leaves both local checkouts unchanged.
- Hardened release safety with destination-path conflict checks, complete ASOC
  pagination requirements, and exact remote-tag verification before deployment;
  aligned the product-mapping copy across the operator UI and documentation.

## 1.3.6 — 2026-08-03

- Exposed the latest persisted GitLab job observation from a successful poll
  as the Scanner Fleet connector `last_seen_at` signal while keeping
  per-instance connection tests separate, and refreshes the fleet projection
  before every read so a completed connection test is visible immediately.
- Made Overview throughput follow the selected timeframe with truthful bucket
  ranges, axis labels, pointer tooltips and keyboard inspection.
- Clarified job-based Overview outcomes and coverage denominators, and replaced
  the split repository detail workbench with a comparable one-row inventory
  whose percentage and supporting counts share the same job-key denominator.
- Strengthened the desktop color system and corrected dense Scanner Fleet
  metadata without reintroducing unsupported mobile layouts.
- Aligned the browser viewport contract with the supported desktop-only
  workspace (1920×1080 and 1440×1000).

## 1.3.5 — 2026-07-30

- Restored the GitLab connection-test boundary: it verifies authentication and
  project discovery without running CI Lint against an arbitrary project.
- Fixed active failed-job recovery when GitLab reports a success with the same
  terminal timestamp as the preceding failure, including signals without a ref.
- Added regression coverage and operator documentation for both contracts.

## 1.3.4 — 2026-07-30

- Fixed terminal GitLab pipeline outcomes so failed or cancelled tracked jobs
  persist the parent pipeline as failed instead of completed.
- Added a tracked startup migration that corrects previously misclassified
  terminal pipelines without changing successful rows.
- Made the Overview success rate explicit about its numerator, denominator and
  `Today` observation window, while separating it from the all-time attention
  backlog.

## 1.3.3 — 2026-07-30

- Restored the production-only corporate build contour: Node and Python base
  images now come from the central mirror, frontend packages come from the
  internal npm group, and both build stages trust the CA bundle prepared by
  internal GitLab CI.
- Restored the internal immutable ScanOps image path in the operator
  environment example and production deployment runbook.

## 1.3.2 — 2026-07-30

- Removed redundant in-page “sections” bars from the short operator and detail
  pages while preserving real route/view tabs and the underlying section
  anchors.
- Completed the production alert environment contract with SMTP, PT AI
  scan-status, long-running job filter and expected-worker settings; release
  preflight now validates channel consistency and requires explicit UI
  verification when no env-backed alert channel is configured.

## 1.3.1 — 2026-07-29

- Replaced the split Failed jobs workbench with a compact, full-width journal
  that keeps status, job, repository, ref, finish time, duration, and source
  pipeline available in each row.
- Replaced the ambiguous pipeline-health orbit with an explicit lifecycle
  distribution whose counts and percentages use the selected observation
  window as their denominator.
- Unified job journal, Active pipelines, and PT AI filter geometry, focus
  states, responsive behavior, and URL-backed interaction contracts.
- Added mobile long-ref overflow coverage and stabilized the deterministic
  local integration recovery gate on Docker Desktop.

## 1.3.0 — 2026-07-29

- Made Signal Control the canonical application interface across Overview,
  executions, failed jobs, Coverage, Products, Repositories, ASOC reporting,
  Scanner Fleet, and integration settings while retaining supported
  compatibility routes.
- Restored full operator workflows in the redesigned UI, including product and
  repository inventory, execution and job drill-down, actionable failed-job
  filters, integration management, and deterministic URL state.
- Hardened live-data behavior with visible stale-state recovery, keyboard-safe
  Quick Action navigation, focus restoration, and accessible filter states.
- Removed product-stat N+1 queries and preserved direct GitLab credential
  fallback when the instance list is explicitly empty.
- Added a deterministic release harness covering backend integration behavior,
  31 UI routes at six viewport sizes, interaction contracts, responsive
  layout, accessibility, and seeded long-data states.

## 1.2.0 — 2026-07-24

- Added explicit credential migration, recovery, and fail-closed acceptance
  behavior for UI-managed integrations.
- Made pipeline lifecycle, pagination, summaries, coverage history, and
  execution health deterministic and operator-focused.
- Unified Target information architecture and navigation, with clearer Scanner
  Fleet and ASOC reporting states.
- Hardened responsive data tables, semantic accessibility, shared UI tokens,
  and English-only operator copy across supported desktop widths.
- Added deterministic seeded browser coverage for three data profiles and a
  local integration-recovery release gate.

## 1.1.0 — 2026-07-22

- Unified execution, coverage, scanner and integration views for the ScanOps release.
- Added encrypted UI-managed GitLab, ASOC, PT AI and Mattermost settings.
- Added safe credential recovery after introducing or rotating
  `INTEGRATION_CREDENTIALS_KEY`: explicitly re-entered credentials replace an
  unreadable legacy payload without requiring the previous encryption key.
- Hardened responsive layout, navigation hierarchy, timestamps and production
  release validation.

## Branch: `feature/trajan-inetgration`

Integration of the standalone **trojan** (GitLab config scanner) project into
**secpipe-monitor** as a new "Config Scan" module. The scanner analyses GitLab
project settings and CI/CD configurations for security misconfigurations.

---

## Architecture Overview

### What was added

The integration brings a parallel scanning subsystem alongside the existing
pipeline poller. It reuses the existing `Product` and `Repository` models —
there is no separate "scan target" concept.

```
secpipe-monitor/
├── app/
│   ├── config_scanner/           # Core scanning engine (from trojan)
│   │   ├── __init__.py
│   │   ├── gitlab_client.py      # Async GitLab API client (httpx)
│   │   ├── rules.py              # Security rules (project settings, CI config)
│   │   └── scanner.py            # Orchestrator: scope resolution → scan → report
│   ├── config_scan_poller.py     # Async poller + manual/URL scan triggers
│   ├── config_scan_service.py    # DB service layer (CRUD for runs/findings)
│   ├── config_scan_reports.py    # JSON + HTML report file generation
│   ├── models/
│   │   └── config_scan.py        # ConfigScanRun, ConfigScanFinding models
│   ├── routers/
│   │   ├── web.py                # New routes under /config-scan/*
│   │   └── api.py                # New API endpoints for scan status/triggers
│   └── templates/
│       ├── config_scan.html              # Overview: all products + scan target input
│       ├── config_scan_product.html      # Per-product: findings + scan history
│       ├── config_scan_run.html          # Run detail: severity cards, results tree
│       └── config_scan_report_export.html # Standalone HTML report template
```

### Data Model

Two new tables (auto-created on startup via `Base.metadata.create_all`):

- **`config_scan_runs`** — one row per scan execution
  - `product_id` FK → `products.id`
  - `status`: queued | running | succeeded | failed
  - `trigger`: poller | manual
  - `scope_path`: the GitLab path used for scanning (preserved for re-runs)
  - `report_json_path`, `report_html_path`: file paths to generated reports
  - `summary_json`: aggregated severity counts
  - `total_projects`, `scanned_projects`, `findings_count`

- **`config_scan_findings`** — individual findings per run
  - `run_id` FK → `config_scan_runs.id`
  - `product_id`, `repository_id` (optional)
  - `severity`, `rule_id`, `title`, `description`, `recommendation`, `detail`

New columns are auto-migrated at startup via `_run_column_migrations()` in
`app/main.py` (uses SQLAlchemy inspector + ALTER TABLE).

### Key Flows

#### 1. Automatic Polling (`ConfigScanPoller`)

Started alongside `PipelinePoller` in `app/main.py` on app startup.
Iterates over all `Product` records, resolves GitLab URL + token, scans.

- Interval: `CONFIG_SCAN_INTERVAL_SECONDS` (default 3600)
- Concurrency: `CONFIG_SCAN_CONCURRENCY` (default 12 parallel project scans)
- Token resolution: `GITLAB_PAT` env var, or `GITLAB_INSTANCES` JSON map

#### 2. Manual Scan via "Run Scan" button

POST `/config-scan/run` with `product_ids` → `trigger_manual_scan()`.
Looks up `scope_path` from the last **succeeded** run for the product and
reuses it. Falls back to `product.path` if no prior run exists.

#### 3. Scan Target (arbitrary URL)

POST `/config-scan/scan-target` with `target_url` form field.
Parses the URL, derives the product name (special "sandbox" root group logic —
if path starts with `sandbox/`, the product name is the second segment).
Finds or creates a `Product`, then calls `trigger_scan_by_url()` with the
full path from the URL.

#### 4. Scope Resolution in Scanner

`scanner.py → _resolve_scope_projects()` tries three strategies:
1. **Group API**: `GET /groups/:path/projects` (fastest, needs group access)
2. **Project API**: `GET /projects/:path` (single project scan)
3. **Namespace search fallback**: `GET /projects?search=...&search_namespaces=true`
   filtered by path prefix (works when token lacks group-level access)

### GitLab Authentication

Tokens are resolved per-host:
- `GITLAB_PAT` — default personal access token
- `GITLAB_INSTANCES` — JSON mapping host → {api_url, token} for multi-instance setups

SSL: `gitlab_client.py` builds an `ssl.SSLContext` from `SSL_CERT_FILE` or
`REQUESTS_CA_BUNDLE` env vars for private CA certificate support.

### UI / Routes

All config scan pages are under `/config-scan/`:

| Route | Method | Description |
|-------|--------|-------------|
| `/config-scan` | GET | Overview: product list with last scan status |
| `/config-scan/products/{id}` | GET | Product detail: findings + history |
| `/config-scan/runs/{id}` | GET | Run detail: severity cards, results tree, findings tables |
| `/config-scan/runs/{id}/report/json` | GET | Download JSON report |
| `/config-scan/runs/{id}/report/html` | GET | Download HTML report |
| `/config-scan/run` | POST | Trigger scan for product_ids (or all) |
| `/config-scan/scan-target` | POST | Scan arbitrary GitLab URL |
| `/config-scan/scan-status` | GET | JSON poll endpoint for live progress |

Navigation tab "Config Scan" added to `base.html`.

### CSS Classes (in `style.css`)

Config scan pages use dedicated CSS classes prefixed `cscan-`:
- `.cscan-status-bar` — horizontal status row
- `.cscan-severity-grid` + `.cscan-sev-card.sev-{critical|high|medium|low|info|stat}` — colored severity count cards
- `.cscan-tree-section` — collapsible results tree wrapper
- `.cscan-group-card` + `.cscan-group-header` — per-repository finding cards
- `.cscan-error-box` — red error display
- `.status-badge.warning`, `.pill.warning` — yellow/amber for medium severity

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_SCAN_ENABLED` | `true` | Enable/disable the config scan poller |
| `CONFIG_SCAN_INTERVAL_SECONDS` | `3600` | Polling interval |
| `CONFIG_SCAN_CONCURRENCY` | `12` | Parallel project scan limit |
| `CONFIG_SCAN_REQUEST_TIMEOUT` | `30` | HTTP request timeout (seconds) |
| `CONFIG_SCAN_MAX_INCLUDE_DEPTH` | `10` | Max CI include recursion depth |
| `CONFIG_SCAN_VERIFY_SSL` | `true` | SSL verification toggle |
| `GITLAB_PAT` | — | Default GitLab personal access token |
| `GITLAB_INSTANCES` | — | JSON: multi-instance host→token mapping |
| `GITLAB_API_URL` | `https://gitlab.com` | Default GitLab API base URL |
| `CONFIG_SCAN_REPORTS_DIR` | `./config_scan_reports` | Report file storage path |

### Known Quirks / Gotchas

1. **"sandbox" group logic**: if the first path segment is `sandbox`, the
   product name is derived from the *second* segment (e.g.
   `sandbox/appsec-templates/security_pipeline` → product `appsec-templates`).
   This is hardcoded in `_normalize_product_group_path()` in `web.py`.

2. **Group API 404**: private GitLab tokens often have project-level access
   but not group-level. The namespace search fallback handles this, but it
   relies on GitLab's `/projects?search=` which may not return all results
   for very large namespaces.

3. **scope_path persistence**: `ConfigScanRun.scope_path` stores the exact
   path used for a scan. "Run Again" reuses the `scope_path` from the last
   succeeded run so that rescans go through the same path (project vs group).

4. **Auto-migration**: new columns are added at startup via ALTER TABLE.
   No downtime needed, but the first request after adding a column may be
   slightly slower.

5. **Report files**: stored on the filesystem at `CONFIG_SCAN_REPORTS_DIR`.
   In containerized deployments, mount a volume if persistence is needed.

---

## Commit History (feature branch)

| Commit | Description |
|--------|-------------|
| `3545cd5` | Initial trojan integration: scanner engine, models, poller, templates |
| `98f4870` | Scan target feature, sandbox group logic, report generation |
| `10d13aa` | UI overhaul: proper CSS classes, severity cards, warning badges |
| `8c37c36` | Namespace search fallback for group API 404 |
| `f930e26` | scope_path persistence: Run Again reuses original scan path |
| *next*    | Poller scope rules; scope backfill from report JSON; Results: product → GitLab path groups → repo table; search; spacious finding cards |
