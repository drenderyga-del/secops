#!/usr/bin/env bash
set -euo pipefail

qa_base_url="${QA_BASE_URL:-http://localhost:18081}"
qa_username="${QA_USERNAME:-demo}"
qa_password="${QA_PASSWORD:-demo}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "${repo_root}/scripts/validate_local_url.py" --name QA_BASE_URL "${qa_base_url}"

if ! curl --silent --show-error --fail \
  --user "${qa_username}:${qa_password}" \
  "${qa_base_url}/api/health" >/dev/null; then
  echo "ScanOps QA target is unavailable at ${qa_base_url}. Start the disposable local stand first." >&2
  exit 2
fi

qa_artifacts_root="${QA_ARTIFACTS_DIR:-qa/artifacts}"
qa_max_safe_links="${QA_MAX_SAFE_LINKS:-0}"
export QA_WORKERS="${QA_WORKERS:-1}"

qa_exit_code=0
QA_ARTIFACTS_DIR="${qa_artifacts_root}/exploratory" \
  npx playwright test qa/e2e/exploratory.spec.ts || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/interactions" \
QA_MAX_SAFE_LINKS="${qa_max_safe_links}" \
  npx playwright test qa/e2e/interactions.spec.ts --project=desktop-1440 || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/overview-contracts" \
  npx playwright test qa/e2e/signal-control.spec.ts \
    --project=desktop-1440 \
    --grep 'repository with no terminal job-key outcome stays neutral|loads the bundled UI and telemetry fonts without a platform fallback|keeps visible interface copy at or above the compact 12px floor|keeps all five river nodes and the track on one baseline|renders an unavailable success rate neutrally when there are no executions|keeps last confirmed data visible when refresh fails and recovers on retry|quick action supports keyboard navigation, focus trap and focus return|keeps throughput bars, axis and keyboard tooltip on the selected timeframe' \
    || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/overview-layout" \
  npx playwright test qa/e2e/overview-layout.spec.ts \
    --project=desktop-1440 \
    || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/integration-focus" \
  npx playwright test qa/e2e/integrations-focus.spec.ts \
    --project=desktop-1440 \
    || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/integration-radar" \
  npx playwright test qa/e2e/integrations-radar.spec.ts \
    --project=desktop-1440 \
    || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/fleet-radar" \
  npx playwright test qa/e2e/scanner-fleet-radar.spec.ts \
    --project=desktop-1440 \
    || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/duration-detail" \
  npx playwright test qa/e2e/duration-detail.spec.ts \
    --project=desktop-1440 \
    || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/mobile-layout" \
  npx playwright test qa/e2e/mobile-layout.spec.ts \
    --project=desktop-1440 \
    || qa_exit_code=$?

QA_ARTIFACTS_DIR="${qa_artifacts_root}/pipeline-detail" \
  npx playwright test qa/e2e/pipeline-detail.spec.ts \
    --project=desktop-1440 \
    || qa_exit_code=$?

exit "${qa_exit_code}"
