#!/usr/bin/env bash
# Export one immutable GitLab release into an existing internal repository.
#
# This wrapper never copies the caller's current working tree. It fetches an
# exact release tag reachable from source main, creates a temporary detached
# worktree at that revision, and exports only that snapshot.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/sync_internal_release.sh [--initialize-manifest] [--remote <git-remote>] <internal-repository> <release-tag>

Examples:
  scripts/sync_internal_release.sh /srv/secpipe-monitor v1.2.0
  scripts/sync_internal_release.sh --remote origin /srv/secpipe-monitor v1.2.0

The destination must be a clean, existing Git repository. The source tag must
be reachable from the source remote's main branch. The script copies the exact
tagged snapshot but deliberately does not commit or push the destination.
EOF
}

remote="personal-gitlab"
initialize_manifest=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --initialize-manifest)
      initialize_manifest=true
      shift
      ;;
    --remote)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      remote="$2"
      shift 2
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
      break
      ;;
  esac
done

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi

destination="$1"
release_ref="$2"
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

if ! git -C "$source_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Source is not a Git checkout: $source_dir" >&2
  exit 2
fi

if [[ ! -d "$destination" ]]; then
  echo "Destination does not exist: $destination" >&2
  exit 2
fi
destination="$(cd "$destination" && pwd -P)"

if [[ "$source_dir" == "$destination" || "$source_dir" == "$destination"/* || "$destination" == "$source_dir"/* ]]; then
  echo "Source and destination repositories must not overlap: $destination" >&2
  exit 2
fi

if ! git -C "$destination" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Destination is not an existing Git repository: $destination" >&2
  exit 2
fi
destination_root="$(git -C "$destination" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$destination_root" ]]; then
  echo "Destination is not an existing Git repository: $destination" >&2
  exit 2
fi
destination_root="$(cd "$destination_root" && pwd -P)"
if [[ "$destination_root" != "$destination" ]]; then
  echo "Destination must be the root of its Git repository, not a subdirectory: $destination" >&2
  exit 2
fi

if [[ -n "$(git -C "$destination" status --porcelain)" ]]; then
  echo "Destination has uncommitted changes: $destination" >&2
  echo "Commit, stash, or discard them before exporting a release." >&2
  exit 2
fi

if ! git -C "$source_dir" remote get-url "$remote" >/dev/null 2>&1; then
  echo "Source remote is not configured: $remote" >&2
  echo "Pass --remote <name> for the GitLab remote on this Mac." >&2
  exit 2
fi

if [[ ! "$release_ref" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Release reference must be an exact vX.Y.Z tag: $release_ref" >&2
  exit 2
fi
git -C "$source_dir" fetch --no-tags "$remote" \
  "refs/heads/main:refs/remotes/$remote/main" \
  "refs/tags/$release_ref:refs/tags/$release_ref"
source_commit="$(git -C "$source_dir" rev-parse --verify "${release_ref}^{commit}" 2>/dev/null)" || {
  echo "Release tag is not available from $remote: $release_ref" >&2
  exit 2
}
if ! git -C "$source_dir" merge-base --is-ancestor "$source_commit" "refs/remotes/$remote/main"; then
  echo "Release tag is not reachable from $remote/main: $release_ref" >&2
  exit 2
fi

temporary_worktree="$(mktemp -d "${TMPDIR:-/tmp}/scanops-release.XXXXXX")"
cleanup() {
  git -C "$source_dir" worktree remove --force "$temporary_worktree" >/dev/null 2>&1 || true
  rm -rf "$temporary_worktree"
}
trap cleanup EXIT

git -C "$source_dir" worktree add --detach --quiet "$temporary_worktree" "$source_commit"
export_args=("$destination")
if [[ "$initialize_manifest" == true ]]; then
  export_args=("--initialize-manifest" "${export_args[@]}")
fi
SCANOPS_RELEASE_SOURCE_SHA="$source_commit" \
SCANOPS_RELEASE_SOURCE_REF="$release_ref" \
SCANOPS_RELEASE_SOURCE_REMOTE="$remote" \
  "$temporary_worktree/scripts/export_internal.sh" "${export_args[@]}"

printf '\nExported release %s (%s) into %s\n' "$release_ref" "$source_commit" "$destination"
printf '\nReview the internal-repository changes:\n'
printf '  git -C %q diff --check\n' "$destination"
printf '  git -C %q status --short\n' "$destination"
printf '  git -C %q diff --stat\n' "$destination"
printf '\nWhen the diff is correct, publish it explicitly:\n'
printf '  git -C %q add -A\n' "$destination"
printf '  git -C %q commit -m %q\n' "$destination" "release: ScanOps $release_ref"
printf '  git -C %q push\n' "$destination"
