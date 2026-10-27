#!/usr/bin/env bash
# Scan all published paths before content so filenames cannot leak into logs.
set -euo pipefail
guard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(git rev-parse --show-toplevel)"
command -v gitleaks >/dev/null || { echo 'Install gitleaks before running this check.' >&2; exit 2; }
scan_dir="$(mktemp -d)"
trap 'rm -rf "$scan_dir"' EXIT
python3 - "$scan_dir" "$guard_dir" <<'PY'
from pathlib import Path
import fnmatch, json, shutil, subprocess, sys
sys.path.insert(0, sys.argv[2])
from public_scan_policy import PATH_SCANNER_RULE, has_path_collisions

out = Path(sys.argv[1])
raw_paths = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).split(b'\0')
# Content scanners do not inspect names, including names deleted from history.
refs = subprocess.check_output(['git', 'for-each-ref', '--format=%(refname)'])
historical = (subprocess.check_output(['git', 'log', '--all', '--format=', '--name-only', '--no-renames', '-z']).split(b'\0')
              if refs.strip() else [])
try:
    names = [raw.decode('utf-8') for raw in [*raw_paths, *historical] if raw]
except UnicodeError:
    raise SystemExit('Unsupported filename encoding; publication blocked')
paths_dir = out / 'paths'; paths_dir.mkdir(mode=0o700)
(paths_dir / 'manifest.jsonl').write_text('\n'.join(json.dumps(name, ensure_ascii=False) for name in names))
config = out / 'path-policy.toml'
config.write_bytes(Path('.gitleaks.toml').read_bytes() + PATH_SCANNER_RULE)
result = subprocess.run(['gitleaks', 'dir', '--redact=100', '--no-banner', '--config', str(config), str(paths_dir)], capture_output=True)
if result.returncode:
    raise SystemExit('Filename secret scan blocked publication; scanner details suppressed')

if has_path_collisions(raw.decode('utf-8') for raw in raw_paths if raw):
    raise SystemExit('Filename alias collision; publication blocked')
snapshot = out / 'snapshot'; snapshot.mkdir(mode=0o700)
for raw in raw_paths:
    if not raw:
        continue
    source = Path(raw.decode('utf-8'))
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
        raise SystemExit('Forbidden local metadata path; publication blocked')
    if source.is_symlink():
        raise SystemExit('Review symbolic links explicitly before publication.')
    if not source.is_file():
        continue
    target = snapshot / source
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
PY
gitleaks dir --redact=100 --no-banner --config .gitleaks.toml "$scan_dir/snapshot"
gitleaks git --redact=100 --no-banner --config .gitleaks.toml --log-opts='--all' .
