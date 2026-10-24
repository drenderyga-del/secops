#!/usr/bin/env bash
# Scan only files Git would publish; local credentials/build caches stay local.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
command -v gitleaks >/dev/null || { echo 'Install gitleaks before running this check.' >&2; exit 2; }
scan_dir="$(mktemp -d)"
trap 'rm -rf "$scan_dir"' EXIT
python3 - "$scan_dir" <<'PY'
from pathlib import Path
import shutil, subprocess, sys
out = Path(sys.argv[1])
for raw in subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).split(b'\0'):
    if not raw:
        continue
    source = Path(raw.decode())
    forbidden = {'.codex', '.agents', '.claude', '.impeccable', '.repowise'}
    if (any(part in forbidden for part in source.parts)
        or source.as_posix().startswith('docs/superpowers/')
        or (source.name.startswith('.env') and source.name != '.env.example')):
        raise SystemExit('Forbidden local metadata path: ' + source.as_posix())

    if source.is_symlink():
        raise SystemExit('Review symbolic links explicitly before publication.')
    if not source.is_file():
        continue
    target = out / source
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
PY
gitleaks dir --redact=100 --no-banner --config .gitleaks.toml "$scan_dir"
gitleaks git --redact=100 --no-banner --config .gitleaks.toml --log-opts='--all' .
