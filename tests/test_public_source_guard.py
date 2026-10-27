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
    (tmp_path / '.gitleaks.toml').write_text('[extend]\nuseDefault = true\n')
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


@pytest.mark.parametrize('location', ['current', 'excluded', 'deleted-history', 'detached-history'])
def test_publication_guard_blocks_filename_credentials_without_echoing_them(tmp_path, location):
    import sys
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', 'config', 'user.name', 'Fixture'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', 'user.email', 'fixture@example.com'], cwd=tmp_path, check=True)
    (tmp_path / '.gitleaks.toml').write_text('[extend]\nuseDefault = true\n')
    secret = 'glpat-' + 'a' * 20
    path = ('cert/' if location == 'excluded' else '') + secret + '.md'
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('Ordinary documentation.\n')
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)
    if location in {'deleted-history', 'detached-history'}:
        subprocess.run(['git', 'commit', '-qm', 'Synthetic fixture'], cwd=tmp_path, check=True)
        subprocess.run(['git', 'rm', path], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-qm', 'Remove fixture'], cwd=tmp_path, check=True)
        if location == 'detached-history':
            subprocess.run(['git', 'checkout', '--detach', '-q'], cwd=tmp_path, check=True)
            refs = subprocess.check_output(['git', 'for-each-ref', '--format=%(refname)'], cwd=tmp_path).decode().splitlines()
            for ref in refs:
                subprocess.run(['git', 'update-ref', '-d', ref], cwd=tmp_path, check=True)
            assert subprocess.check_output(['git', 'for-each-ref'], cwd=tmp_path) == b''
    fake_bin = tmp_path / 'bin'; fake_bin.mkdir()
    scanner = fake_bin / 'gitleaks'
    scanner.write_text('#!' + sys.executable + '\nimport pathlib,sys\n'
                       'root=pathlib.Path(sys.argv[-1])\n'
                       'raise SystemExit(1 if root.is_dir() and any("glpat-" in p.read_text() '
                       'for p in root.rglob("manifest.jsonl")) else 0)\n')
    scanner.chmod(0o755)
    result = subprocess.run(['bash', str(ROOT / 'scripts/check_public_source.sh')], cwd=tmp_path,
                            env={**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH']},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Filename secret scan blocked publication' in result.stderr
    assert secret not in result.stdout + result.stderr
