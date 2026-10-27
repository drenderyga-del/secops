#!/usr/bin/env bash
# Scan only files Git would publish; local credentials/build caches stay local.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
command -v gitleaks >/dev/null || { echo 'Install gitleaks before running this check.' >&2; exit 2; }
scan_dir="$(mktemp -d)"
trap 'rm -rf "$scan_dir"' EXIT
python3 - "$scan_dir" <<'PY'
from pathlib import Path
import fnmatch, shutil, subprocess, sys
out = Path(sys.argv[1])
for raw in subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).split(b'\0'):
    if not raw:
        continue
    source = Path(raw.decode())
    local_dirs = {
        '.git', '.gitlab', '.codex', '.agents', '.claude', '.impeccable', '.repowise',
        '.playwright-cli', 'playwright-report', 'test-results', 'cert', 'certs',
    }
    local_names = ('.gitlab*', 'CODEX_CONTEXT.md', '.codex*', 'codex-*', '*.local.*')
    sensitive_extensions = ('*.pem', '*.key', '*.p12', '*.pfx', '*.crt', '*.cer',
                            '*.dump', '*.sql.gz', '*.db', '*.sqlite', '*.sqlite3')
    path = source.as_posix().casefold()
    if (any(part.casefold() in local_dirs for part in source.parts)
        or any(fnmatch.fnmatchcase(part.casefold(), pattern.casefold())
               for part in source.parts for pattern in local_names)
        or any(fnmatch.fnmatchcase(source.name.lower(), pattern)
               for pattern in sensitive_extensions)
        or path.startswith(('docs/superpowers/', 'qa/artifacts/', 'output/playwright/'))
        or (source.name.casefold().startswith('.env') and source.name.casefold() != '.env.example')):
        raise SystemExit('Forbidden local metadata path: ' + path)

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
