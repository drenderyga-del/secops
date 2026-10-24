#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x .venv/bin/python ]]; then
  python_bin=".venv/bin/python"
else
  python_bin="python3"
fi

mode="${1:-fast}"

validate_demo_compose() {
  docker compose -f docker-compose.demo.yml config --quiet
}

run_unit() {
  env \
    -u UI_AUTH_USERNAME \
    -u UI_AUTH_PASSWORD \
    -u DATABASE_URL \
    "$python_bin" -m pytest -q -m "not needs_db"
}

run_integration() {
  local compose_project="${SCANOPS_QA_PROJECT:-scanops-verification}"
  (
    cleanup_integration() {
      docker compose \
        --project-name "$compose_project" \
        -f docker-compose.demo.yml \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
    }
    trap cleanup_integration EXIT
    docker compose \
      --project-name "$compose_project" \
      -f docker-compose.demo.yml \
      run --build --rm \
      -e UI_AUTH_USERNAME= \
      -e UI_AUTH_PASSWORD= \
      -e CONFIG_SCAN_ENABLED=true \
      -e CONFIG_SCAN_RUN_BACKGROUND_SCHEDULER=false \
      app python -m pytest -q
    docker compose \
      --project-name "$compose_project" \
      -f docker-compose.demo.yml \
      run --rm \
      -e UI_AUTH_USERNAME= \
      -e UI_AUTH_PASSWORD= \
      -e PYTHONPATH=/app \
      app python scripts/seed_demo_data.py --profile kitchen-sink --reset --anchor now
    docker compose \
      --project-name "$compose_project" \
      -f docker-compose.demo.yml \
      run --rm \
      -e UI_AUTH_USERNAME= \
      -e UI_AUTH_PASSWORD= \
      -e SCANOPS_REQUIRE_DEMO_SEED=1 \
      app python -m pytest -q tests/test_demo_reset_schema.py
  )
}

run_browser() {
  if [[ ! -d node_modules/@playwright/test ]]; then
    echo "Browser QA dependencies are missing. Run npm install first." >&2
    return 2
  fi
  npm run qa:e2e:typecheck
  ./scripts/run_browser_qa.sh
}

case "$mode" in
  unit)
    run_unit
    ;;
  fast)
    run_unit
    validate_demo_compose
    ;;
  integration)
    validate_demo_compose
    run_integration
    ;;
  browser)
    run_browser
    ;;
  full)
    validate_demo_compose
    run_integration
    run_browser
    ;;
  release)
    env_file="${2:-}"
    if [[ -z "$env_file" || ! -f "$env_file" ]]; then
      echo "Usage: scripts/qa.sh release /absolute/path/to/internal.env" >&2
      exit 2
    fi
    validate_demo_compose
    run_integration
    run_browser
    "$python_bin" scripts/preflight_internal.py --env-file "$env_file"
    ;;
  *)
    echo "Usage: scripts/qa.sh unit|fast|integration|browser|full|release [env-file]" >&2
    exit 2
    ;;
esac
