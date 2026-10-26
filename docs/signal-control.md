# Signal Control workspace

Signal Control is the ScanOps production workspace on top of the existing domain
model. Its read-only view models expose persisted executions, jobs, pipelines,
products, repositories, control bindings, scanner runs, deliveries, worker
heartbeats and integration health. Detailed domain actions open the canonical
operator workbench. Integration configuration uses the existing protected backend
endpoints directly from Signal Control.

## Navigation

The main sections have canonical product URLs:

```text
/
/dashboard/duration
/executions
/logs
/logs?status=failed
/pipelines
/pipelines/{active_pipeline_id}
/config-scan
/iac
/ptai-scans
/coverage
/targets?level=product
/targets?level=repository
/targets/operator?level=product
/targets/operator?level=repository
/scanners
/scanners/operator
/integrations
/integrations?integration=asoc
/integrations?integration=gitlab
```

`/executions` shows the durable execution lifecycle. `/logs` is the complete job
journal with category, repository, job, status and timeframe filters.
Executions, Job Journal/Failed Jobs, Active Pipelines, Config Guard findings,
PT AI scans and Repositories share one bottom pagination control: result range,
`Page N of M`, previous/next links and a `25`/`50`/`100` page-size selector.
`page` and `page_size` persist in the URL. Changing a filter or page size resets
the page to one; browser Back restores the selected state.
`/logs?status=failed` uses the same contract as a recovery queue.
Legacy `/next?view=...` and `/classic...` URLs are permanent redirect aliases,
not separate product interfaces.

Actions requiring the extended server-rendered workbench, such as scan launches,
manual synchronization and advanced diagnostics, open the same product shell at
`/config-scan/operator`, `/iac/operator`, `/coverage/operator`,
`/scanners/operator` or `/targets/operator`.
`/overview/operator` is a `308` compatibility alias for `/`, not a separate
workbench. The scanner operator workspace retains adapter connection,
latest-run and seven-day workload drill-downs; canonical `/scanners` remains the
runtime registry. Target administration retains Normalize paths, Cleanup
duplicates and Delete product; canonical Products and Repositories remain
read-only inventories. Pipeline/PT AI filters, pagination, product/repository
browsing and integration management work directly on the canonical React pages.
Explicit `/pipelines/operator`, `/ptai-scans/operator` and
`/integrations/operator` routes retain server-rendered collection snapshots.

The primary link from `/pipelines` opens the internal detail route using
`ActivePipeline.id`; a separate `GitLab ↗` action opens the external pipeline.
Details show only persisted jobs, group them by their actual stage names, and
mark `tracked_only` when registration restricted the job-name list.
GitLab's retained jobs snapshot does not include the declared CI stage list.
Stage names are therefore grouped alphabetically for a stable diagnostic view;
this grouping does not represent execution order. Detail and mutation routes
continue to use existing backend contracts.

### Product registry and ASOC mapping

Products is the primary inventory and ASOC mapping view. Synchronization reads
ASOC product cards and matches monitored products by normalized exact GitLab
URLs in the ASOC description. Case, `.git` and a trailing slash do not change
identity. ASOC repository membership does not establish the link; the slug is
external context only. Repository counts in this projection count GitLab links
in descriptions, not ASOC membership.

The registry retains ASOC-only and ScanOps-only rows and distinguishes `mapped`,
`partial`, `conflict`, `missing_target` and `no_repositories`. Cards removed from
ASOC retain their original connection date in quarterly history but are not
active inventory. Conflicting cards do not publish an unambiguous target mapping.
Manual synchronization is the only path that can confirm bulk removal. It
requires same-origin operator Basic auth and cannot be invoked by a CI bearer.
Scheduled pipeline-worker synchronization is independent of UI authentication
and never removes cards: any snapshot containing removals waits for manual sync.
The last successful pipeline is the maximum terminal timestamp among retained
`ActivePipeline.status=completed` rows; a newer failed pipeline does not replace
it. The drill-down remains `/pipelines?product=<local id>`.

Quarterly reporting belongs to the registry. The product connection date is the
ASOC card's `created` time in `UI_TIMEZONE`, not its first synchronization time.
A card can precede discovery of its GitLab group. `waiting N days` and `overdue`
after `PRODUCT_SYNC_GRACE_DAYS` apply only while the card has never been linked:
there is neither a link date nor a linked group. A previously linked card that
later degrades is not waiting. Cards linked before date tracking show
`Unknown · linked before date tracking`.

`period=YYYY-QN&mode=new` selects products connected during the quarter;
`mode=total` selects all connected by quarter end, including cards removed later.
The status summary does not depend on the status filter. XLSX export at
`/api/ui/products/export.xlsx` uses the same projection as the table. Repository
coverage comes from a GitLab audit of every project in the group, including
repositories that have never sent a security job.
Cards open `/targets/products/asoc/{asoc_id}`; groups without a card open
`/targets/products/{id}`. Pipeline-worker synchronization runs every
`ASOC_PRODUCT_SYNC_INTERVAL_MINUTES` and records the latest attempt. A snapshot
that would remove even one card is not applied automatically.
`/reports`, `/reports/operator` and `/reports/export.xlsx` return `308` redirects
to the registry/export, converting `year` and `quarter` into `period`.

### Overview and compatibility routes

Canonical Overview handles timeframes at `/`.
`/overview/operator?timeframe=today` redirects with `308` to `/?timeframe=today`,
preserving the query. Workspace navigation preserves timeframe/tool controls,
and browser Back returns to the previous workspace state. The attention queue
uses the existing `IacService.dashboard()` `critical + high` backlog.
The documented pipeline success-rate projection counts terminal GitLab pipelines
for the current UTC day: `completed / (completed + failed)`. Numerator,
denominator and the `Today` window appear beside the percentage. Attention is a
separate current backlog that may include unresolved signals from older periods;
it is not the denominator of that rate.

Unfiltered legacy `/audit` redirects to `/coverage`. With `only_problems`,
`selected_product_ids`, `status`, `status_filter` or `run_id`, it redirects to
`/coverage/operator` with the same query parameters, because that workbench
implements the complete legacy filter contract. Canonical IaC links directly
to `/iac/repositories`.

Workspace navigation updates browser history. Quick action opens by button or
`⌘K` / `Ctrl+K`. Existing narrow-screen styles below 780 px place labelled
primary navigation at the bottom and bring the active section into view;
visible controls have at least 44 px touch targets. In the Executions and Failed
jobs master/detail views, selecting a row reveals and scrolls to its detail panel
while the long list retains its own bounded scroll container. Supported product
acceptance widths are defined in the verification section below.

### Products, repositories and integration settings

Products and Repositories have distinct responsibilities:

- Product cards (`/targets/products/asoc/{asoc_id}`) and group pages
  (`/targets/products/{id}`) show ASOC links and synchronization, repository scan
  states, pipelines and ASOC deliveries. `/targets/products/{id}/operator`
  retains product maintenance and control bindings.
- Browse repositories preserves the product filter in the URL.
- Repository 360 shows ownership, coverage, execution timelines, normalized
  artifacts, delivery evidence and producer versions.

Integration browsing and configuration stay within one workspace:

- Selecting a connector opens the right settings panel without leaving
  `/integrations`.
- ASOC, PT AI and Mattermost expose their configuration fields. GitLab lists
  connections and opens only one editor at a time.
- Secret fields are write-only and never returned by the UI API.
- Save changes keeps the panel open so Test connection can follow.
- Closing or switching with unsaved changes requires confirmation.
- `integration` and `instance` parameters make the selection addressable and
  preserve browser history.
- Mattermost connection testing explicitly warns that it sends one visible
  test message to the configured channel.

## Live data contracts

The workspace uses authenticated GET endpoints:

| Endpoint | Purpose |
| --- | --- |
| `/api/ui/overview` | Operational signal, topology, attention queue and throughput. |
| `/api/ui/executions` | Durable execution lifecycle and artifacts; `25`/`50`/`100` pagination. |
| `/api/ui/failed-jobs` | Unresolved failed-job evidence. |
| `/api/ui/job-journal` | Filtered job journal, pagination, autocomplete options and summary. |
| `/api/ui/pipelines` | Active/observed GitLab pipeline lifecycle; `25`/`50`/`100` pagination. |
| `/api/ui/pipelines/{active_pipeline_id}` | Persisted header, completeness, ordered stages, jobs and external links. |
| `/api/ui/config-guard` | Configuration scans and server-paginated findings. |
| `/api/ui/iac` | IaC runs, severities and persisted scanner details. |
| `/api/ui/ptai` | PT AI runs and actual scanner state; `25`/`50`/`100` pagination. |
| `/api/ui/coverage` | Product audit coverage and control bindings. |
| `/api/ui/products` | Quarterly ASOC card registry, status summary and groups without cards. |
| `/api/ui/products/widgets/{asoc_id}`, `/api/ui/products/targets/{id}` | Card/group drill-down: links, repositories, pipelines and ASOC deliveries. |
| `/api/ui/products/export.xlsx` | XLSX for the same slice as the table. |
| `/api/ui/repositories` | Repository inventory, batch-calculated health, server-side `q`/`product` filters and `25`/`50`/`100` pagination; bounded legacy `limit<=1000` remains supported. |
| `/api/ui/repositories/{id}` | Repository 360. |
| `/api/ui/scanner-fleet` | Worker heartbeats and capacity. |
| `/api/ui/integrations` | Safe configuration metadata, connection counts and aggregate connector health. |

Collections refresh automatically. Loading, empty and error states are explicit;
missing scanner evidence is not replaced with an invented root cause. Read-only
cards do not masquerade as buttons. Actions outside the Signal Control API open
the existing canonical or operator workbench, including `/targets/operator` for
product maintenance.

Integration settings use existing POST contracts:
`/integrations/{key}/save`, `/integrations/{key}/test`,
`/integrations/{key}/disable` and `/integrations/gitlab/instances/...`.
Responses are handled inside the workspace without navigating to a legacy UI.
`/api/ui/integrations` never returns PATs, tokens or stored secret values.

## Verification

Focused interaction suite:

```bash
QA_BASE_URL=http://127.0.0.1:18081 \
QA_WORKERS=2 \
npx playwright test qa/e2e/signal-control.spec.ts
```

The full visual/accessibility audit of sections and integration settings runs
at the two supported desktop widths: 1920 and 1440 px. Narrower tablet and mobile
layouts are outside the product contract:

```bash
QA_BASE_URL=http://127.0.0.1:18081 \
QA_ROUTE_TAGS=signal-control \
QA_WORKERS=2 \
QA_ARTIFACTS_DIR=qa/artifacts/signal-control \
npx playwright test qa/e2e/exploratory.spec.ts
```

Inspect `findings.json` as well as the exit code. A release candidate must have
no P0/P1 findings, runtime/network errors, horizontal overflow or text below 12 px.
