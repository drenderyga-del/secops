#!/usr/bin/env bash
# Deploy one internal release tag to the ScanOps host over SSH.
#
# The private key is supplied by path and is never copied into the repository
# or sent through stdin. The remote checkout must be clean. Database is left
# running; only the application and worker services are recreated by default.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/deploy_internal_release.sh [options]

Required:
  --host HOST             Hostname, for example scanops.example.com.
  --path PATH             Remote checkout, for example
                          /opt/secpipe-monitor/secpipe-monitor/.
  --tag TAG               Internal release tag, for example v2.4.0.

Options:
  --user USER             SSH user (default: local SSH default).
  --port PORT             SSH port (default: 22).
  --identity-file PATH    Private SSH key outside the repository.
  --branch NAME           Internal branch (default: v2.0).
  --compose-file PATH     Compose file (default: docker-compose.yml).
  --health-url URL        Remote health URL (default:
                          http://127.0.0.1:8081/api/health).
  --services CSV          Services to pull/recreate (default:
                          app,pipeline_worker,iac_worker).
  --dry-run               Print the remote actions without connecting.
  -h, --help              Show this help.

SSH host-key verification is strict. Add the host to known_hosts before the
first real deploy; the script never accepts a new host key automatically.
EOF
}

host=""
remote_user=""
remote_path=""
release_tag=""
ssh_port="22"
identity_file=""
internal_branch="v2.0"
compose_file="docker-compose.yml"
health_url="http://127.0.0.1:8081/api/health"
services_csv="app,pipeline_worker,iac_worker"
dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      host="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      remote_user="$2"
      shift 2
      ;;
    --path)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      remote_path="$2"
      shift 2
      ;;
    --tag)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      release_tag="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      ssh_port="$2"
      shift 2
      ;;
    --identity-file)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      identity_file="$2"
      shift 2
      ;;
    --branch)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      internal_branch="$2"
      shift 2
      ;;
    --compose-file)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      compose_file="$2"
      shift 2
      ;;
    --health-url)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      health_url="$2"
      shift 2
      ;;
    --services)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      services_csv="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      echo "Unexpected argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$host" || -z "$remote_path" || -z "$release_tag" ]]; then
  echo "--host, --path, and --tag are required." >&2
  usage
  exit 2
fi
if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Release tag must use vX.Y.Z: $release_tag" >&2
  exit 2
fi
if [[ ! "$ssh_port" =~ ^[0-9]+$ || "$ssh_port" -lt 1 || "$ssh_port" -gt 65535 ]]; then
  echo "SSH port must be an integer from 1 to 65535." >&2
  exit 2
fi
if [[ -z "$services_csv" ]]; then
  echo "At least one service is required." >&2
  exit 2
fi
IFS=',' read -r -a services <<< "$services_csv"
for service in "${services[@]}"; do
  if [[ ! "$service" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid compose service name: $service" >&2
    exit 2
  fi
done

if [[ -n "$identity_file" ]]; then
  if [[ ! -f "$identity_file" ]]; then
    echo "SSH identity file does not exist: $identity_file" >&2
    exit 2
  fi
  identity_mode="$(stat -c '%a' "$identity_file" 2>/dev/null || stat -f '%Lp' "$identity_file")"
  case "$identity_mode" in
    400|600) ;;
    *)
      echo "SSH identity file must have mode 400 or 600: $identity_file" >&2
      exit 2
      ;;
  esac
fi

target="$host"
if [[ -n "$remote_user" ]]; then
  target="$remote_user@$host"
fi

echo "Host:              $target:$ssh_port"
echo "Remote checkout:   $remote_path"
echo "Release:           $release_tag"
echo "Internal branch:   $internal_branch"
echo "Compose services:  ${services[*]}"
if [[ "$dry_run" == true ]]; then
  echo "Dry run: no SSH connection or remote changes were made."
  exit 0
fi

ssh_args=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=15
  -p "$ssh_port"
)
if [[ -n "$identity_file" ]]; then
  ssh_args+=( -i "$identity_file" )
fi

ssh "${ssh_args[@]}" "$target" bash -s -- \
  "$remote_path" "$release_tag" "$internal_branch" "$compose_file" "$health_url" \
  "${services[@]}" <<'REMOTE_SCRIPT'
set -euo pipefail

remote_path="$1"
release_tag="$2"
internal_branch="$3"
compose_file="$4"
health_url="$5"
shift 5
services=("$@")

cd "$remote_path"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Remote path is not a Git repository: $remote_path" >&2
  exit 2
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Remote checkout has uncommitted changes: $remote_path" >&2
  exit 2
fi

current_branch="$(git symbolic-ref --quiet --short HEAD || true)"
git fetch --tags origin "$internal_branch"
if [[ -z "$current_branch" ]]; then
  git switch "$internal_branch" 2>/dev/null || git switch --track -c "$internal_branch" "origin/$internal_branch"
  current_branch="$internal_branch"
fi
if [[ "$current_branch" != "$internal_branch" ]]; then
  echo "Remote checkout must use branch $internal_branch; found $current_branch." >&2
  exit 2
fi

git pull --ff-only origin "$internal_branch"
remote_tag_lines="$(git ls-remote --tags --exit-code origin \
  "refs/tags/$release_tag" "refs/tags/$release_tag^{}" || true)"
remote_tag_object="$(printf '%s\n' "$remote_tag_lines" | awk \
  -v direct="refs/tags/$release_tag" \
  -v peeled="refs/tags/$release_tag^{}" \
  '$2 == peeled { print $1; found = 1; exit } $2 == direct { direct_sha = $1 } END { if (!found && direct_sha) print direct_sha }')"
if [[ -z "$remote_tag_object" ]]; then
  echo "Release tag does not exist on origin: $release_tag" >&2
  exit 2
fi
git fetch --no-tags origin "refs/tags/$release_tag:refs/tags/$release_tag"
remote_tag_commit="$(git rev-parse --verify "${remote_tag_object}^{commit}" 2>/dev/null || true)"
local_tag_commit="$(git rev-parse --verify "refs/tags/$release_tag^{commit}" 2>/dev/null || true)"
if [[ -z "$remote_tag_commit" || "$local_tag_commit" != "$remote_tag_commit" ]]; then
  echo "Local release tag does not match origin/$release_tag: $release_tag" >&2
  exit 2
fi
if ! git merge-base --is-ancestor "$release_tag" "origin/$internal_branch"; then
  echo "Release tag is not reachable from origin/$internal_branch: $release_tag" >&2
  exit 2
fi
git switch --detach "$release_tag"

if [[ ! -f .env ]]; then
  echo "Remote .env is missing; refusing to infer the production image." >&2
  exit 2
fi
configured_image="$(awk -F= '$1 == "SECPIPE_IMAGE" {print substr($0, index($0, "=") + 1); exit}' .env)"
configured_image="${configured_image#\"}"
configured_image="${configured_image%\"}"
configured_image="${configured_image#\'}"
configured_image="${configured_image%\'}"
if [[ "$configured_image" != */*:* ]]; then
  echo "SECPIPE_IMAGE in .env must be an explicit registry image with a tag." >&2
  exit 2
fi
image_repository="${configured_image%:*}"
release_image="${image_repository}:${release_tag}"

if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  echo "Neither docker compose nor docker-compose is available on the host." >&2
  exit 2
fi

SECPIPE_IMAGE="$release_image" "${compose[@]}" -f "$compose_file" pull "${services[@]}"
SECPIPE_IMAGE="$release_image" "${compose[@]}" -f "$compose_file" up -d --force-recreate "${services[@]}"

health_body=""
for attempt in {1..60}; do
  if health_body="$(curl --silent --show-error --fail --max-time 10 "$health_url" 2>/dev/null)"; then
    if printf '%s' "$health_body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"healthy"'; then
      echo "Healthcheck passed: $health_body"
      "${compose[@]}" -f "$compose_file" ps
      echo "Deployed $release_tag ($release_image)."
      exit 0
    fi
  fi
  sleep 5
done

echo "Healthcheck did not become healthy: $health_url" >&2
"${compose[@]}" -f "$compose_file" ps >&2 || true
"${compose[@]}" -f "$compose_file" logs --tail=100 app >&2 || true
exit 1
REMOTE_SCRIPT
