"""Local agent metadata must fail publication even without a secret match."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('path', ['.impeccable/critique/private.md', '.codex/private.md', 'docs/superpowers/plans/private.md', '.env.private'])
def test_publication_guard_rejects_forced_tracked_local_metadata(tmp_path, path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('Synthetic local note without a credential pattern.\n')
    subprocess.run(['git', 'add', '-f', path], cwd=tmp_path, check=True)
    fake_bin = tmp_path / 'bin'; fake_bin.mkdir()
    scanner = fake_bin / 'gitleaks'; scanner.write_text('#!/bin/sh\nexit 0\n'); scanner.chmod(0o755)
    result = subprocess.run(['bash', str(ROOT / 'scripts/check_public_source.sh')], cwd=tmp_path,
                            env={**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH']},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Forbidden local metadata path' in result.stderr
