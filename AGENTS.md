# ScanOps contributor instructions

- Canonical source: git@github.com:drenderyga-del/secops.git.
- Keep main stable. Start focused feat/*, fix/* or docs/* branches from current
  develop and use pull requests into develop. Do not force-push shared branches
  or commit directly to main/develop. Release changes reach main through a PR.
- Inspect status and diff before editing. Preserve unrelated work. Never copy
  private history, credentials, customer data, trust bundles or deployment
  configuration into this public source repository.
- Run npm ci --prefix frontend and npm run build --prefix frontend before tests
  that exercise the React shell. Use scripts/qa.sh fast for backend/config work,
  integration for database/worker changes, browser for UI changes. Inspect browser
  artifacts. Report actual commands, results, skips and external limitations.
- Database changes require ordered SQL migrations and meaningful focused tests.
- After implementation, validation, commit, push and PR creation, request a fresh
  independent read-only reviewer. Use security_reviewer for auth, credentials,
  migrations, concurrency or destructive changes; otherwise use reviewer.
  Provide exact source/target SHAs. Fix findings and obtain a new review for the
  new HEAD. Never merge using a verdict for an older source or target commit.
- Merge only after required checks, independent CLEAN review, current source and
  target SHA verification, no conflicts and no unresolved blocking discussions.
  Respect GitHub approvals and branch protections. Prefer squash into develop;
  preserve real main ancestry when back-merging a release into develop.
- Rebuild the demo from the deliverable checkout with docker compose -f
  docker-compose.demo.yml up -d --build. Wait for app health and seed completion,
  check HTTP, and leave http://127.0.0.1:18081 running for handoff. State branch,
  commit, credentials and the limits of demo validation.
- Keep scripts/check_public_source.sh green. Its exceptions cover exact synthetic
  test fixtures only. Keep TLS verification and provider redirect blocking on.
- External agent credentials belong only to /api/v1/agent/. Sharing job context,
  reviewing proposals and configuring ASOC destinations require operator Basic
  auth and same-origin CSRF checks. Never expose raw job payloads, execute agent
  commands or apply agent patches automatically.
- Distinguish report acceptance from verified finding processing. Report adapters
  do not replace the existing Vampy product registry, PT AI or IaC worker flow.
- Private deployment repositories own their CI, registry and secrets. This source
  repository runs checks only and does not publish container images or deploy.

- Report receipts bind immutable destination generations and non-secret target
  snapshots. Fail replays after reconfiguration; treat provider POST 5xx outcomes
  as unknown. New destination encryption never falls back to the CI bearer.
- Product namespace depth is configured explicitly through
  PRODUCT_NESTED_NAMESPACES and has no organization-specific default.
