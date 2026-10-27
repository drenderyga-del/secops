"""Local agent metadata must fail publication even without a secret match."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('path', [
    '.impeccable/critique/private.md', '.codex/private.md',
    '.ENV', '.Env.production', 'CERT/private.txt', 'QA/ARTIFACTS/report.txt',
    '.Codex/private.md', '.AGENTS/state.txt', '.GITLAB-CI.yml',
    'docs/superpowers/plans/private.md', '.env.private', 'CODEX_CONTEXT.md',
    'codex-session/notes.md', '.codex-backup/state.json',
    'playwright-report/index.html', 'test-results/screen.png',
    '.playwright-cli/state.yml', 'output/playwright/screen.png',
    'qa/artifacts/report.json', 'qa/e2e/manifests/routes.local.json',
    'cert/internal-identity.txt', 'certs/identity.txt', 'exports/identity.crt',
    'exports/identity.PEM', 'exports/client.key', 'exports/client.p12',
    'exports/client.pfx', 'exports/identity.cer', 'exports/data.dump',
    'exports/data.sql.gz', 'exports/data.db', 'exports/data.sqlite',
    'exports/data.sqlite3',
])
def test_publication_guard_rejects_forced_tracked_local_metadata(tmp_path, path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'\x00Synthetic local data without a credential pattern.\xff')
    subprocess.run(['git', 'add', '-f', path], cwd=tmp_path, check=True)
    fake_bin = tmp_path / 'bin'; fake_bin.mkdir()
    scanner = fake_bin / 'gitleaks'; scanner.write_text('#!/bin/sh\nexit 0\n'); scanner.chmod(0o755)
    result = subprocess.run(['bash', str(ROOT / 'scripts/check_public_source.sh')], cwd=tmp_path,
                            env={**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH']},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Forbidden local metadata path' in result.stderr
