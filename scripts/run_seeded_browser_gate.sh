#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

qa_base_url="${QA_BASE_URL:-http://127.0.0.1:${DEMO_APP_PORT:-18081}}"
qa_mock_url="${QA_MOCK_URL:-http://127.0.0.1:${SCANOPS_MOCK_PORT:-18080}}"
python3 scripts/validate_local_url.py --name QA_BASE_URL "${qa_base_url}"
python3 scripts/validate_local_url.py --name QA_MOCK_URL "${qa_mock_url}"

recovery_compose="${QA_RECOVERY_COMPOSE:-docker-compose.integration-recovery.yml}"
if [[ ! -f "${recovery_compose}" ]]; then
  echo "Missing ${recovery_compose}. Sync Task326 before running the seeded browser gate." >&2
  exit 2
fi

if [[ -n "${QA_ARTIFACTS_DIR:-}" ]]; then
  mkdir -p "${QA_ARTIFACTS_DIR}"
  qa_artifacts_dir="$(cd "${QA_ARTIFACTS_DIR}" && pwd)"
else
  qa_artifacts_dir="${TMPDIR:-/tmp}/scanops-browser-gate/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "${qa_artifacts_dir}"
fi
export QA_ARTIFACTS_DIR="${qa_artifacts_dir}"
export SECPIPE_IMAGE="${SECPIPE_IMAGE:-scanops-demo:local}"

compose_project="${QA_COMPOSE_PROJECT:-scanops-browser-gate-${PPID}}"
compose=(
  docker compose
  --project-name "${compose_project}"
  --file docker-compose.demo.yml
  --file "${recovery_compose}"
  --file docker-compose.browser-gate.yml
)

cleanup() {
  if [[ "${QA_KEEP_STAND:-0}" != "1" ]]; then
    "${compose[@]}" down --volumes --remove-orphans >/dev/null
  fi
}
trap cleanup EXIT

if [[ ! -d node_modules/@playwright/test ]]; then
  npm ci
fi
npx playwright install chromium

"${compose[@]}" build app
"${compose[@]}" up --detach --wait --wait-timeout 180 postgres qa_mock app

qa_username="${QA_USERNAME:-demo}"
qa_password="${QA_PASSWORD:-demo}"

for attempt in $(seq 1 60); do
  if curl --silent --fail --user "${qa_username}:${qa_password}" "${qa_base_url}/api/health" >/dev/null; then
    break
  fi
  if [[ "${attempt}" == "60" ]]; then
    echo "ScanOps did not become healthy at ${qa_base_url}." >&2
    exit 2
  fi
  sleep 2
done

gate_exit=0
for profile in empty kitchen-sink volume; do
  profile_dir="${qa_artifacts_dir}/${profile}"
  mkdir -p "${profile_dir}"

  curl --silent --show-error --fail \
    --request POST \
    "${qa_mock_url}/__control__/reset" >/dev/null

  "${compose[@]}" run --rm seed \
    python scripts/seed_demo_data.py \
      --profile "${profile}" \
      --reset \
      --manifest "/qa-artifacts/${profile}/manifest.json" \
      --mock-base-url "http://qa_mock:18080"

  QA_BASE_URL="${qa_base_url}" \
  QA_USERNAME="${qa_username}" \
  QA_PASSWORD="${qa_password}" \
  QA_ROUTE_MANIFEST="${profile_dir}/manifest.json" \
  QA_ARTIFACTS_DIR="${profile_dir}/browser" \
    ./scripts/run_browser_qa.sh || gate_exit=$?
done

echo "Seeded browser gate artifacts: ${qa_artifacts_dir}"
exit "${gate_exit}"
