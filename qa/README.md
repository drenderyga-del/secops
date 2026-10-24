# Seeded browser release gate

The local release gate builds a disposable ScanOps stand, seeds three deterministic data profiles, and audits the rendered UI with Chromium. It is read-only from the browser: only same-origin `GET` and `HEAD` requests are allowed.

## One-command run

Prerequisites are Docker Compose, Node.js/npm, and Task326's tracked `docker-compose.integration-recovery.yml`.

```bash
npm run qa:release-gate
```

The command installs pinned npm dependencies and Chromium when needed, starts the local app plus Task326's `qa_mock`, and runs these profiles in order:

- `empty`: first-run and empty-state coverage.
- `kitchen-sink`: mixed statuses, long values, equal timestamps, and representative detail records.
- `volume`: larger tables and pagination/overflow pressure.

The seed points integrations only at `http://qa_mock:18080`. Before every profile the runner calls Task326's local-only `POST /__control__/reset`; it does not own or duplicate the mock implementation. The disposable Compose project and volumes are removed on exit. Set `QA_KEEP_STAND=1` only while diagnosing locally.

## Audit contract

Each profile supplies both the static route inventory and seeded detail routes. ScanOps is a desktop operational console; the exploratory pass runs every route at the two supported desktop widths:

- 1920×1080
- 1440×1000

The 1440px viewport is the conservative lower bound for the supported MacBook
Pro-class workspace. Narrow-tablet and mobile layouts are outside the product
contract and are not release-gate targets.

The 1440px interaction pass exercises every distinct safe same-origin `GET` link shape and every enabled option of every `GET` select. It checks the destination HTTP status, reload persistence, and browser Back restoration. Repeated entity IDs collapse to one route shape; query and hash states remain distinct. Browser mutations, external origins, downloads, and action-like links are blocked.

The Products registry additionally runs a `period × mode` browser matrix over the four most recent quarters. Every case asserts the selected slice mode, captures initial/reload HTTP errors, and verifies Back to the unfiltered registry. The retired `/reports`, `/reports/operator` and `/classic/reports` routes are covered as compatibility redirects to Products.

The gate records:

- route-level 4xx/5xx responses and transport failures;
- uncaught runtime and console errors;
- axe accessibility violations, keyboard traps, and missing focus indicators;
- viewport/text overflow, table collisions, undersized controls, and duplicate DOM IDs;
- screenshots, traces, videos, browser diagnostics, structured findings, and Markdown/HTML reports.

The release policy is **zero P0 and zero P1 findings**. The custom reporter returns a failed final status whenever either severity is present, even if Playwright assertions passed.

## Evidence location

Artifacts default to a timestamped directory outside the repository:

```text
${TMPDIR:-/tmp}/scanops-browser-gate/YYYYMMDDTHHMMSSZ/
├── empty/
├── kitchen-sink/
└── volume/
```

Each profile contains `manifest.json` and separate `browser/exploratory` and `browser/interactions` reports. Override with an absolute `QA_ARTIFACTS_DIR`; repository-local QA artifacts are ignored and must never be committed.

Useful local controls:

```bash
QA_ARTIFACTS_DIR=/absolute/path/to/evidence npm run qa:release-gate
QA_WORKERS=2 npm run qa:release-gate
QA_MAX_SAFE_LINKS=5 npm run qa:release-gate
```

## Focused harness checks

Validate route discovery and TypeScript loading without starting the stand:

```bash
QA_ROUTE_MANIFEST=qa/e2e/manifests/routes.example.json npm run qa:e2e:list
```

Run against an already seeded local stand:

```bash
QA_ROUTE_MANIFEST=/absolute/path/manifest.json \
QA_ARTIFACTS_DIR=/absolute/path/browser-evidence \
./scripts/run_browser_qa.sh
```

The DOM geometry checks are heuristics. Screenshots remain the source for triaging a finding, but the P0/P1 exit policy is automatic and deterministic.

---

# Local integration recovery contract

`qa/mock_server.py` is a deterministic, outbound-free HTTP double for the
GitLab, ASOC, PT AI and Mattermost contracts used by ScanOps. It supports
worker and credential recovery acceptance; browser route sweeps belong to the
separate seeded browser gate.

Run it directly:

```bash
python qa/mock_server.py --host 127.0.0.1 --port 18080
curl http://127.0.0.1:18080/healthz
```

Or run the same contract from a locally available ScanOps image:

```bash
SECPIPE_IMAGE=scanops:local \
  docker compose -f docker-compose.integration-recovery.yml up --wait qa_mock
```

The stable prefixes are:

- GitLab: `http://127.0.0.1:18080/gitlab/api/v4`
- ASOC: `http://127.0.0.1:18080/asoc/api`
- PT AI: `http://127.0.0.1:18080/ptai`
- Mattermost: `http://127.0.0.1:18080/mattermost/hooks/local-mattermost-token`

Dummy tokens are exported as `qa.mock_server.DUMMY_CREDENTIALS`. They are not
production examples and the mock rejects other credentials.

## Scenario control

The default scenario is `success`. Select a failure for one connector:

```bash
curl -X POST http://127.0.0.1:18080/__control__/scenario \
  -H 'Content-Type: application/json' \
  -d '{"service":"gitlab","scenario":"rate_limited"}'
```

Supported scenarios are `success`, `unauthorized`, `forbidden`,
`rate_limited`, `malformed`, `timeout` and `transport`. `delay_seconds`
controls the timeout response delay. `service` may also be `all`.

State and reset endpoints:

```bash
curl http://127.0.0.1:18080/__control__/state
curl -X POST http://127.0.0.1:18080/__control__/reset
```

State contains only method, redacted path, connector, status and body byte
count. It never stores headers or request bodies; the Mattermost token segment
is replaced with `***`. `outbound_requests` is always zero because the mock
contains no outbound client.

Run the focused acceptance:

```bash
python -m pytest -q tests/test_local_integration_recovery.py
```
