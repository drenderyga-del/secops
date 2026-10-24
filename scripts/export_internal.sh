#!/usr/bin/env bash
# Copy the deployable ScanOps source into an existing internal repository.
# Source Git metadata is excluded and the destination's GitLab CI definition
# is preserved, so this is safe to run after `git pull` on the developer workstation.
set -euo pipefail

initialize_manifest=false
if [[ $# -eq 2 && "$1" == "--initialize-manifest" ]]; then
  initialize_manifest=true
  shift
elif [[ $# -ne 1 ]]; then
  echo "Usage: $0 [--initialize-manifest] /absolute/path/to/internal/secpipe-monitor" >&2
  exit 2
fi

destination="$1"
if [[ ! -d "$destination" ]]; then
  echo "Destination does not exist: $destination" >&2
  exit 2
fi

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
destination="$(cd "$destination" && pwd -P)"

if [[ "$source_dir" == "$destination" || "$source_dir" == "$destination"/* || "$destination" == "$source_dir"/* ]]; then
  echo "Source and destination repositories must not overlap: $destination" >&2
  exit 2
fi

# This script is an implementation detail of sync_internal_release.sh. Refuse
# to copy the caller's live tree, even if a tracked file contains accidental
# credentials or unreviewed work. The wrapper supplies these values from its
# detached, exact-tag worktree after checking release ancestry.
source_commit="$(git -C "$source_dir" rev-parse HEAD)"
source_ref="${SCANOPS_RELEASE_SOURCE_REF:-}"
source_remote="${SCANOPS_RELEASE_SOURCE_REMOTE:-}"
if [[ "${SCANOPS_RELEASE_SOURCE_SHA:-}" != "$source_commit" || -z "$source_ref" || -z "$source_remote" ]]; then
  echo "Direct exports are disabled; use scripts/sync_internal_release.sh with an exact release tag." >&2
  exit 2
fi
if [[ -n "$(git -C "$source_dir" status --porcelain --untracked-files=all)" ]]; then
  echo "Refusing to export a dirty source worktree: $source_dir" >&2
  exit 2
fi
if [[ ! "$source_ref" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || ! git -C "$source_dir" show-ref --verify --quiet "refs/tags/$source_ref" \
  || [[ "$(git -C "$source_dir" rev-parse "refs/tags/$source_ref^{commit}")" != "$source_commit" ]]; then
  echo "Export source must be the exact vX.Y.Z tag named by the release wrapper." >&2
  exit 2
fi
if ! git -C "$source_dir" merge-base --is-ancestor "$source_commit" "refs/remotes/$source_remote/main"; then
  echo "Export source tag is not reachable from $source_remote/main: $source_ref" >&2
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

manifest_file="$destination/.scanops-export-manifest"
manifest_tmp="$(mktemp "${TMPDIR:-/tmp}/scanops-export-manifest.XXXXXX")"
manifest_probe="$(mktemp -d "${TMPDIR:-/tmp}/scanops-export-probe.XXXXXX")"
cleanup() {
  rm -f -- "$manifest_tmp"
  rm -rf -- "$manifest_probe"
}
trap cleanup EXIT

if [[ -L "$manifest_file" || ( -e "$manifest_file" && ! -f "$manifest_file" ) ]]; then
  echo "Destination export manifest must be a regular file: $manifest_file" >&2
  exit 2
fi

bootstrap_manifest=false
if [[ "$initialize_manifest" == true ]]; then
  if [[ -e "$manifest_file" ]]; then
    echo "Destination export manifest already exists: $manifest_file" >&2
    exit 2
  fi
  if [[ -n "$(git -C "$destination" status --porcelain)" ]]; then
    echo "Manifest initialization requires a clean destination repository: $destination" >&2
    exit 2
  fi
  bootstrap_manifest=true
fi

# Build a newline-delimited list of source-owned files from an empty probe
# directory. The probe keeps the manifest independent from rsync's itemized
# output and avoids mutating either repository while calculating the snapshot.
rsync --dry-run --archive --out-format='%n' \
  --exclude-from="$source_dir/scripts/internal-export.exclude" \
  "$source_dir/" "$manifest_probe/" \
  | sed -e '/\/$/d' -e '/^\.$/d' \
  | LC_ALL=C sort -u > "$manifest_tmp"

is_preserved_path() {
  case "$1" in
    .env|.env.*|.git|.git/*|.gitlab-ci.yml|.scanops-export-manifest|.scanops-release|docker-compose.yml)
      return 0
      ;;
  esac
  return 1
}

validate_manifest_path() {
  local path="$1"
  case "$path" in
    /*|.|..|../*|*/../*|*/..|*//*|./*|*/./*)
      echo "Unsafe path in destination export manifest: $path" >&2
      exit 2
      ;;
  esac
}

check_manifest_parent() {
  local path="$1"
  local parent="$destination"
  local index last_index
  local -a parts
  IFS='/' read -r -a parts <<< "$path"
  last_index=$((${#parts[@]} - 1))
  for ((index = 0; index < last_index; index++)); do
    parent="$parent/${parts[index]}"
    if [[ -L "$parent" ]]; then
      echo "Refusing to remove through a destination symlink: $parent" >&2
      exit 2
    fi
  done
}

path_is_recorded() {
  [[ -f "$manifest_file" ]] && grep -Fqx -- "$1" "$manifest_file"
}

check_destination_parent() {
  local path="$1"
  local parent="$destination"
  local index last_index
  local -a parts
  IFS='/' read -r -a parts <<< "$path"
  last_index=$((${#parts[@]} - 1))
  for ((index = 0; index < last_index; index++)); do
    parent="$parent/${parts[index]}"
    if [[ -L "$parent" ]]; then
      echo "Refusing to export through a destination symlink: $parent" >&2
      exit 2
    fi
    if [[ -e "$parent" && ! -d "$parent" ]]; then
      echo "Source export path has a non-directory destination parent: $parent" >&2
      exit 2
    fi
  done
}

path_is_tracked() {
  git -C "$destination" ls-files --error-unmatch -- "$1" >/dev/null 2>&1
}

path_is_source_tracked() {
  git -C "$source_dir" ls-files --error-unmatch -- "$1" >/dev/null 2>&1
}

# Validate all old manifest entries before removing anything from the
# destination. This also makes a malformed ownership record fail closed.
if [[ -f "$manifest_file" ]]; then
  while IFS= read -r old_path || [[ -n "$old_path" ]]; do
    [[ -n "$old_path" ]] || continue
    validate_manifest_path "$old_path"
  done < "$manifest_file"
fi

# Validate all new source paths before removing anything from the destination.
# A path that already exists without a prior source-ownership record belongs to
# the internal repository by default; silently taking it over would make a
# later manifest cleanup destructive. File/directory transitions and symlink
# targets are rejected for the same reason.
while IFS= read -r current_path || [[ -n "$current_path" ]]; do
  [[ -n "$current_path" ]] || continue
  validate_manifest_path "$current_path"
  if ! path_is_source_tracked "$current_path"; then
    echo "Refusing to export an untracked source path: $current_path" >&2
    echo "Commit it or add it to $source_dir/scripts/internal-export.exclude first." >&2
    exit 2
  fi
  check_destination_parent "$current_path"
  target="$destination/$current_path"
  if [[ -L "$target" ]]; then
    echo "Source export path conflicts with a destination symlink: $current_path" >&2
    exit 2
  fi
  if path_is_recorded "$current_path"; then
    if [[ -d "$target" ]]; then
      echo "Source file would replace a destination directory: $current_path" >&2
      exit 2
    fi
  elif [[ -e "$target" ]]; then
    if [[ "$bootstrap_manifest" != true ]] || ! path_is_tracked "$current_path"; then
      echo "Source export path already belongs to destination: $current_path" >&2
      echo "Inspect it or record ownership in $manifest_file before exporting." >&2
      exit 2
    fi
    if [[ -d "$target" ]]; then
      echo "Source file would replace a destination directory: $current_path" >&2
      exit 2
    fi
  fi
done < "$manifest_tmp"

# Delete only files that a previous export explicitly recorded as source
# owned. Destination-only files and the preserved CI/credential files are
# never covered by this cleanup, so this is intentionally narrower than
# rsync --delete.
if [[ -f "$manifest_file" ]]; then
  while IFS= read -r old_path || [[ -n "$old_path" ]]; do
    [[ -n "$old_path" ]] || continue
    validate_manifest_path "$old_path"
    is_preserved_path "$old_path" && continue
    if ! grep -Fqx -- "$old_path" "$manifest_tmp"; then
      target="$destination/$old_path"
      if [[ -e "$target" || -L "$target" ]]; then
        check_manifest_parent "$old_path"
        if [[ -d "$target" && ! -L "$target" ]]; then
          echo "Refusing to remove stale exported directory: $target" >&2
          exit 2
        fi
        rm -f -- "$target"
      fi
    fi
  done < "$manifest_file"
fi

# --checksum: a changed file with the same size and modification second as
# the destination copy must still be exported (rsync's quick check skips it).
rsync -a --checksum \
  --exclude-from="$source_dir/scripts/internal-export.exclude" \
  "$source_dir/" "$destination/"

chmod 0644 "$manifest_tmp"
cp "$manifest_tmp" "$manifest_file"

echo "ScanOps deployable source copied to $destination"
echo "Preserved: destination .git and .gitlab-ci.yml"
echo "Tracked source-owned files in $manifest_file"
if [[ "$bootstrap_manifest" == true ]]; then
  echo "Initialized ownership from this clean destination's tracked source paths."
fi
