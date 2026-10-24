#!/usr/bin/env bash
# Publish one exact source release into the internal GitLab repository.
#
# The source checkout is this repository. The destination is an existing,
# clean checkout of the internal repository. The command creates the internal
# release commit and tag, then pushes both atomically. It never force-pushes.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/publish_internal_release.sh [options] /absolute/path/to/internal-repo

Required options:
  --source-tag TAG       Existing source release tag on source main.
  --internal-tag TAG     New internal release tag, for example v2.4.0.

Instead of --internal-tag, use one of:
  --bump patch|minor|major

Other options:
  --source-remote NAME   Source remote (default: origin).
  --internal-remote NAME Internal repository remote (default: origin).
  --internal-branch NAME Internal release branch (default: v2.0).
  --dry-run              Validate and print the planned release only.
  -h, --help             Show this help.

The internal checkout must be clean and already have the requested branch
checked out. The source tag must be reachable from source remote main.
EOF
}

source_remote="origin"
internal_remote="origin"
internal_branch="v2.0"
source_tag=""
internal_tag=""
bump=""
dry_run=false
destination=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-tag|--source-ref)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      source_tag="$2"
      shift 2
      ;;
    --internal-tag)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      internal_tag="$2"
      shift 2
      ;;
    --bump)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      bump="$2"
      shift 2
      ;;
    --source-remote)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      source_remote="$2"
      shift 2
      ;;
    --internal-remote)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      internal_remote="$2"
      shift 2
      ;;
    --internal-branch)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      internal_branch="$2"
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
      if [[ -n "$destination" ]]; then
        echo "Unexpected argument: $1" >&2
        usage
        exit 2
      fi
      destination="$1"
      shift
      ;;
  esac
done

if [[ -z "$destination" || -z "$source_tag" ]]; then
  echo "Destination and --source-tag are required." >&2
  usage
  exit 2
fi
if [[ -n "$internal_tag" && -n "$bump" ]]; then
  echo "Use either --internal-tag or --bump, not both." >&2
  exit 2
fi
if [[ -z "$internal_tag" && -z "$bump" ]]; then
  echo "One of --internal-tag or --bump is required." >&2
  usage
  exit 2
fi
if [[ -n "$bump" && ! "$bump" =~ ^(patch|minor|major)$ ]]; then
  echo "--bump must be patch, minor, or major." >&2
  exit 2
fi

version_tag_re='^v([0-9]+)\.([0-9]+)\.([0-9]+)$'
if [[ ! "$source_tag" =~ $version_tag_re ]]; then
  echo "Source tag must use vX.Y.Z: $source_tag" >&2
  exit 2
fi
if [[ -n "$internal_tag" && ! "$internal_tag" =~ $version_tag_re ]]; then
  echo "Internal tag must use vX.Y.Z: $internal_tag" >&2
  exit 2
fi

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
if [[ -n "$(git -C "$source_dir" status --porcelain)" ]]; then
  echo "Source checkout has uncommitted changes: $source_dir" >&2
  exit 2
fi
if [[ -n "$(git -C "$destination" status --porcelain)" ]]; then
  echo "Destination has uncommitted changes: $destination" >&2
  exit 2
fi

if ! git -C "$source_dir" remote get-url "$source_remote" >/dev/null 2>&1; then
  echo "Source remote is not configured: $source_remote" >&2
  exit 2
fi
if ! git -C "$destination" remote get-url "$internal_remote" >/dev/null 2>&1; then
  echo "Internal remote is not configured: $internal_remote" >&2
  exit 2
fi
source_remote_url="$(git -C "$source_dir" remote get-url "$source_remote")"
internal_remote_url="$(git -C "$destination" remote get-url "$internal_remote")"

current_branch="$(git -C "$destination" symbolic-ref --quiet --short HEAD || true)"
if [[ "$current_branch" != "$internal_branch" ]]; then
  echo "Destination must have branch $internal_branch checked out; found ${current_branch:-detached HEAD}." >&2
  exit 2
fi

if [[ "$dry_run" == true ]]; then
  validation_dir="$(mktemp -d "${TMPDIR:-/tmp}/scanops-publish-dry-run.XXXXXX")"
  cleanup_dry_run() {
    rm -rf -- "$validation_dir"
  }
  trap cleanup_dry_run EXIT
  validation_repo="$validation_dir/repository.git"
  git init --bare --quiet "$validation_repo"

  # Fetch into a disposable bare repository. A dry run must validate the
  # exact source tag and destination ancestry without changing either local
  # checkout's remote-tracking refs or FETCH_HEAD.
  git -C "$validation_repo" fetch --no-tags --quiet "$source_remote_url" \
    "refs/heads/main:refs/remotes/source/main" \
    "refs/tags/$source_tag:refs/tags/$source_tag"
  source_commit="$(git -C "$validation_repo" rev-parse --verify "$source_tag^{commit}")"
  if ! git -C "$validation_repo" merge-base --is-ancestor "$source_commit" "refs/remotes/source/main"; then
    echo "Source tag is not reachable from $source_remote/main: $source_tag" >&2
    exit 2
  fi

  if ! git ls-remote --exit-code "$internal_remote_url" "refs/heads/$internal_branch" >/dev/null; then
    echo "Internal branch does not exist on $internal_remote: $internal_branch" >&2
    exit 2
  fi
  git -C "$validation_repo" fetch --no-tags --quiet "$internal_remote_url" \
    "refs/heads/$internal_branch:refs/remotes/internal/$internal_branch"
  git -C "$validation_repo" fetch --no-tags --quiet "$destination" \
    "HEAD:refs/remotes/destination/head"
  internal_commit="$(git -C "$validation_repo" rev-parse --verify "refs/remotes/internal/$internal_branch^{commit}")"
  destination_commit="$(git -C "$validation_repo" rev-parse --verify "refs/remotes/destination/head^{commit}")"
  if ! git -C "$validation_repo" merge-base --is-ancestor "$internal_commit" "$destination_commit" \
    && ! git -C "$validation_repo" merge-base --is-ancestor "$destination_commit" "$internal_commit"; then
    echo "Destination and $internal_remote/$internal_branch have diverged." >&2
    exit 2
  fi
else
  git -C "$source_dir" fetch --no-tags "$source_remote" \
    "refs/heads/main:refs/remotes/$source_remote/main" \
    "refs/tags/$source_tag:refs/tags/$source_tag"
  source_commit="$(git -C "$source_dir" rev-parse --verify "$source_tag^{commit}")"
  if ! git -C "$source_dir" merge-base --is-ancestor "$source_commit" "refs/remotes/$source_remote/main"; then
    echo "Source tag is not reachable from $source_remote/main: $source_tag" >&2
    exit 2
  fi

  git -C "$destination" fetch "$internal_remote" \
    "refs/heads/$internal_branch:refs/remotes/$internal_remote/$internal_branch"
fi

remote_tags="$(git -C "$destination" ls-remote --tags --refs "$internal_remote" 'refs/tags/v*')"
if [[ -z "$internal_tag" ]]; then
  latest_tag="$(printf '%s\n' "$remote_tags" | awk '{sub("refs/tags/", "", $2); print $2}' | grep -E "$version_tag_re" | sort -V | tail -n 1 || true)"
  if [[ -z "$latest_tag" ]]; then
    latest_tag="$(git -C "$destination" tag --list 'v*' | grep -E "$version_tag_re" | sort -V | tail -n 1 || true)"
  fi
  if [[ -z "$latest_tag" ]]; then
    echo "Cannot calculate a release tag: no existing vX.Y.Z tag was found." >&2
    exit 2
  fi
  if [[ ! "$latest_tag" =~ $version_tag_re ]]; then
    echo "Cannot calculate a release tag from the latest internal tag: $latest_tag" >&2
    exit 2
  fi
  major="${BASH_REMATCH[1]}"
  minor="${BASH_REMATCH[2]}"
  patch="${BASH_REMATCH[3]}"
  case "$bump" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
  esac
  internal_tag="v${major}.${minor}.${patch}"
fi

if git -C "$destination" show-ref --verify --quiet "refs/tags/$internal_tag"; then
  echo "Internal tag already exists locally: $internal_tag" >&2
  exit 2
fi
if printf '%s\n' "$remote_tags" | awk '{print $2}' | grep -Fxq "refs/tags/$internal_tag"; then
  echo "Internal tag already exists on $internal_remote: $internal_tag" >&2
  exit 2
fi

echo "Source release:   $source_tag ($source_commit)"
echo "Internal release: $internal_tag"
echo "Internal branch:  $internal_branch"
echo "Destination:      $destination"
if [[ "$dry_run" == true ]]; then
  echo "Dry run: no files were exported, committed, tagged, or pushed."
  exit 0
fi

git -C "$destination" merge --ff-only "refs/remotes/$internal_remote/$internal_branch"

bash "$source_dir/scripts/sync_internal_release.sh" \
  --remote "$source_remote" "$destination" "$source_tag"

provenance_file="$destination/.scanops-release"
provenance_tmp="$(mktemp "${TMPDIR:-/tmp}/scanops-release.XXXXXX")"
cleanup() {
  rm -f "$provenance_tmp"
}
trap cleanup EXIT
{
  printf 'source_tag=%s\n' "$source_tag"
  printf 'source_commit=%s\n' "$source_commit"
  printf 'internal_tag=%s\n' "$internal_tag"
  printf 'published_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} > "$provenance_tmp"
mv "$provenance_tmp" "$provenance_file"

git -C "$destination" diff --check
git -C "$destination" add -A
git -C "$destination" diff --cached --check
if git -C "$destination" diff --cached --quiet; then
  echo "Export produced no internal changes; refusing to create an empty release." >&2
  exit 2
fi
if [[ -z "$(git -C "$destination" config user.name)" || -z "$(git -C "$destination" config user.email)" ]]; then
  echo "Internal checkout must have git user.name and user.email configured." >&2
  exit 2
fi

git -C "$destination" commit -m "release: ScanOps $internal_tag"
git -C "$destination" tag -a "$internal_tag" -m "ScanOps $internal_tag"
git -C "$destination" push --atomic "$internal_remote" \
  "$internal_branch" "refs/tags/$internal_tag"

echo "Published $internal_tag to $internal_remote/$internal_branch."
