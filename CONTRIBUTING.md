# Contributing to ScanOps

Clone the public repository and start one focused branch from current develop:

```bash
git clone https://github.com/drenderyga-del/secops.git
cd secops
git switch develop
git pull --ff-only
git switch -c feat/short-description
```

Use fix/* for bugs and docs/* for documentation. Keep main stable; submit pull
requests to develop and prefer squash merges after independent review. Use a
short-lived release branch and a reviewed PR into main for releases, then merge
that exact main state back into develop without squash. Never rewrite shared
history or bypass repository protections.

## Local validation

Use Python 3.12, Node 22.13 or newer in the Node 22 line, Docker Compose, and Git.
Install dependencies in an isolated virtual environment and build the UI shell:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix frontend
npm run build --prefix frontend
scripts/qa.sh fast
```

- scripts/qa.sh unit: tests without a required PostgreSQL instance.
- scripts/qa.sh fast: unit checks and demo Compose validation.
- scripts/qa.sh integration: disposable Docker/PostgreSQL tests and seed checks.
- scripts/qa.sh browser: browser checks against a running seeded target; see
  [qa/README.md](qa/README.md) for installation and target configuration.

Run meaningful focused tests first. Database and worker changes require the
integration suite; UI changes require browser checks and artifact inspection.
Record exact commands, results, skipped checks and anything not verified.
The public GitHub workflow runs checks and builds; it does not publish images.

Before a PR or public push, install Gitleaks and run:

```bash
scripts/check_public_source.sh
git diff --check
```

Read [SECURITY.md](SECURITY.md) before reporting vulnerabilities or sharing
examples. Never submit credentials, private infrastructure details, customer
data, unreviewed logs, local agent state or database exports. All test data must
be synthetic. Keep narrow exact fixture exceptions; do not exempt whole folders.

## Demo handoff

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

Wait for app health and seed completion. Open http://127.0.0.1:18081 with
`demo` / `demo`, and record the running branch/commit and health result. This
stand uses synthetic data; it cannot verify live vendor services or production
permissions. Keep it running for manual inspection.

## Review and design contracts

Follow [AGENTS.md](AGENTS.md) for independent review and exact-commit merge
checks. Security-sensitive changes require a fresh security reviewer. Keep
existing product behavior and update operator documentation when it changes.

Global server-template UI tokens live in app/static/css/tokens.css. Route styles
must not add a competing :root or shrink visible text below 12px. Preserve
keyboard focus and reduced-motion support. Run tests/test_shared_ui_tokens.py
when changing shared or route CSS.

## Private deployments

Deployments own their registry, CI and credentials outside this repository. The
optional scripts/sync_internal_release.sh flow exports an exact tagged main
snapshot while preserving destination-owned files. Always pass the source
remote explicitly (normally --remote origin). Test an export in a disposable
checkout before using it in a private environment; see [the operator reference](docs/operations-reference.ru.md) for details.
