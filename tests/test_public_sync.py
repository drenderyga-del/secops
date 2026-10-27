"""Publication boundaries and three-way preservation for private-to-public sync."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare_public_sync.py'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()


def commit(repo):
    git(repo, 'add', '-A'); git(repo, 'commit', '-qm', 'fixture')
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def repos(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.mkdir()
    git(source, 'init', '-q'); git(source, 'config', 'user.name', 'Fixture')
    git(source, 'config', 'user.email', 'fixture@example.com')
    (source / '.gitleaks.toml').write_text('[extend]\nuseDefault = true\n')
    (source / 'app.py').write_text('first = 1\n\nlast = 1\n')
    (source / 'README.md').write_text('# Original documentation\n')
    (source / '.env.example').write_text('API_URL=https://example.com\n')
    base = commit(source)
    public = tmp_path / 'public'
    subprocess.run(['git', 'clone', '-q', str(source), str(public)], check=True)
    git(public, 'config', 'user.name', 'Fixture'); git(public, 'config', 'user.email', 'fixture@example.com')
    (public / 'README.md').write_text('# Concise English introduction\n')
    (public / 'public-only.md').write_text('Public documentation.\n')
    commit(public)
    binaries = tmp_path / 'bin'; binaries.mkdir()
    scanner = binaries / 'gitleaks'; scanner.write_text('#!/bin/sh\nexit 0\n'); scanner.chmod(0o755)
    monkeypatch.setenv('PATH', str(binaries) + os.pathsep + os.environ['PATH'])
    return source, public, base


def run_export(tmp_path, repos, config=None, extra=()):
    source, public, base = repos
    rules = tmp_path / 'private-rules.json'
    rules.write_text(json.dumps(config or {'schema_version': 1})); rules.chmod(0o600)
    output = tmp_path / 'snapshot'; report = tmp_path / 'report.json'
    result = subprocess.run([sys.executable, str(SCRIPT), '--source', str(source),
                             '--base-ref', base, '--ref', 'HEAD', '--public', str(public),
                             '--output', str(output), '--rules', str(rules), '--report', str(report), *extra],
                            capture_output=True, text=True)
    return result, output, json.loads(report.read_text()) if report.exists() else None


def test_export_sanitizes_snapshot_excludes_private_files_and_preserves_public_docs(tmp_path, repos):
    source, public, _ = repos
    (source / 'app.py').write_text('url = "https://registry.corp.example/project"\n')
    (source / 'README.md').write_text('# Private front page\n')
    (source / '.env.private').write_text('TOP_SECRET=not-for-publication\n')
    (source / '.gitlab-ci.yml').write_text('private-pipeline: {}\n')
    commit(source)
    before = git(public, 'rev-parse', 'HEAD')
    result, output, report = run_export(tmp_path, repos, {
        'schema_version': 1,
        'replacements': [{'id': 'host', 'find': 'registry.corp.example', 'replace': 'registry.example.com'}],
        'forbidden_literals': ['corp.example'],
    })
    assert result.returncode == 0, result.stdout
    assert report['status'] == 'prepared_for_review'
    assert (output / 'app.py').read_text() == 'url = "https://registry.example.com/project"\n'
    assert (output / 'README.md').read_bytes() == (public / 'README.md').read_bytes()
    assert (output / 'public-only.md').is_file()
    assert not (output / '.git').exists() and not (output / '.env.private').exists()
    assert not (output / '.gitlab-ci.yml').exists()
    assert report['replacement_counts'] == {'host': 1}
    assert git(public, 'rev-parse', 'HEAD') == before
    assert git(public, 'status', '--porcelain') == ''
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / 'report.json').stat().st_mode) == 0o600
    assert 'corp.example' not in json.dumps(report)


def test_three_way_merge_preserves_independent_public_code_changes(tmp_path, repos):
    source, public, _ = repos
    (public / 'app.py').write_text('first = 2\n\nlast = 1\n'); commit(public)
    (source / 'app.py').write_text('first = 1\n\nlast = 3\n'); commit(source)
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 0, result.stdout
    assert (output / 'app.py').read_text() == 'first = 2\n\nlast = 3\n'


def test_conflicting_public_changes_block_without_writing_snapshot(tmp_path, repos):
    source, public, _ = repos
    (public / 'app.py').write_text('first = 2\n'); commit(public)
    (source / 'app.py').write_text('first = 3\n'); commit(source)
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 1 and report['error'] == 'content_conflicts'
    assert report['conflicts'] == ['app.py'] and not output.exists()
    assert (public / 'app.py').read_text() == 'first = 2\n'


@pytest.mark.parametrize('path,data,config', [
    ('internal.txt', b'PRIVATE_MARKER', {'forbidden_literals': ['PRIVATE_MARKER']}),
    ('new.png', b'\x89PNG\x00private screenshot', {}),
    ('new.md', 'Документ'.encode(), {}),
])
def test_residual_internal_data_unknown_binaries_and_nonenglish_docs_block(tmp_path, repos, path, data, config):
    source, public, _ = repos
    (source / path).write_bytes(data); commit(source)
    result, output, report = run_export(tmp_path, repos, {'schema_version': 1, **config})
    assert result.returncode == 1 and report['error'] == 'publication_review_required'
    assert not output.exists() and data.decode(errors='ignore') not in result.stdout


def test_scanner_failure_blocks_before_replacement_and_does_not_echo_secret(tmp_path, repos, monkeypatch):
    scanner = tmp_path / 'bin/gitleaks'
    scanner.write_text('#!/bin/sh\necho PRIVATE_VALUE >&2\nexit 1\n')
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 1 and report['error'] == 'secret_scan_failed'
    assert not output.exists() and 'PRIVATE_VALUE' not in result.stdout + result.stderr


def test_source_deletion_does_not_delete_modified_public_file(tmp_path, repos):
    source, public, _ = repos
    (public / 'app.py').write_text('public_feature = True\n'); commit(public)
    (source / 'app.py').unlink(); commit(source)
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 1 and report['error'] == 'content_conflicts'
    assert not output.exists()


def test_output_and_report_cannot_be_written_into_either_repository(tmp_path, repos):
    source, public, _ = repos
    forbidden_report = public / 'private-report.json'
    result, output, report = run_export(tmp_path, repos, extra=('--report', str(forbidden_report)))
    assert result.returncode == 1 and not forbidden_report.exists() and not output.exists()
    assert git(public, 'status', '--porcelain') == ''


def test_excluded_source_symlink_is_not_followed_and_other_symlinks_block(tmp_path, repos):
    source, public, _ = repos
    (source / 'link').symlink_to('/etc/passwd'); commit(source)
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 1 and report['error'] == 'symlink_or_submodule'
    assert not output.exists()


def test_only_exactly_approved_new_binary_is_exported(tmp_path, repos):
    import hashlib
    source, public, _ = repos
    data = b'\x89PNG\x00reviewed synthetic image'
    (source / 'image.png').write_bytes(data); commit(source)
    result, output, report = run_export(tmp_path, repos, {
        'schema_version': 1, 'approved_binary_sha256': [hashlib.sha256(data).hexdigest()],
    })
    assert result.returncode == 0, result.stdout
    assert (output / 'image.png').read_bytes() == data


def test_source_deletion_removes_only_unchanged_source_owned_file(tmp_path, repos):
    source, public, _ = repos
    (source / 'app.py').unlink(); commit(source)
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 0, result.stdout
    assert report['removed_paths'] == ['app.py'] and not (output / 'app.py').exists()
    assert (output / 'public-only.md').exists()


def test_unrecognized_rule_keys_fail_closed(tmp_path, repos):
    result, output, report = run_export(tmp_path, repos, {'schema_version': 1, 'forbiden_literals': ['typo']})
    assert result.returncode == 1 and report['error'] == 'invalid_rules'
    assert not output.exists()


def test_incoming_source_cannot_replace_public_secret_scanning_policy(tmp_path, repos):
    source, public, _ = repos
    (public / 'scripts').mkdir()
    (public / 'scripts/check_public_source.sh').write_text('#!/bin/sh\nexit 1\n')
    commit(public)
    (source / '.gitleaks.toml').write_text('[allowlist]\nregexes = [".*"]\n')
    (source / 'scripts').mkdir()
    (source / 'scripts/check_public_source.sh').write_text('#!/bin/sh\nexit 0\n')
    commit(source)
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 0, result.stdout
    for name in ('.gitleaks.toml', 'scripts/check_public_source.sh'):
        assert (output / name).read_bytes() == (public / name).read_bytes()
        assert name in report['preserved_public_paths']


def test_private_artifact_exclusions_are_case_insensitive(tmp_path, repos):
    source, public, _ = repos
    private_paths = ('.ENV', '.Env.production', 'CERT/private.txt', 'QA/artifacts/report.txt',
                     '.GITLAB/private.txt', '.GitLab-CI.yml', 'EXPORT.LOCAL.JSON')
    for name in private_paths:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Private deployment metadata.\n')
    commit(source)
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 0, result.stdout
    for name in private_paths:
        assert not (output / name).exists()
        assert name in report['excluded_paths']


@pytest.mark.parametrize('excluded_path', [False, True])
def test_token_in_filename_blocks_before_filename_is_written_to_report(tmp_path, repos, excluded_path):
    source, public, _ = repos
    secret = 'glpat-' + 'a' * 20
    name = ('cert/' if excluded_path else '') + secret + '.md'
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Ordinary text.\n')
    commit(source)
    # Model a content-only scanner: only the explicit manifest reveals the name.
    scanner = tmp_path / 'bin/gitleaks'
    scanner.write_text('#!' + sys.executable + '\nimport pathlib,sys\n'
                       'root=pathlib.Path(sys.argv[-1])\n'
                       'raise SystemExit(1 if any("glpat-" in p.read_text() '
                       'for p in root.rglob("*") if p.is_file()) else 0)\n')
    result, output, report = run_export(tmp_path, repos)
    assert result.returncode == 1 and report['error'] == 'secret_scan_failed'
    assert not output.exists()
    assert secret not in json.dumps(report) + result.stdout + result.stderr


@pytest.mark.parametrize('name', ['.GIT/config', '.Codex/settings.json', '.AGENTS/state.json',
                                  '.GIT/objects/ab/data', '.Env', 'QA/ARTIFACTS/results.json'])
def test_case_variant_metadata_paths_are_rejected_without_touching_filesystem(name):
    import runpy
    assert runpy.run_path(str(SCRIPT))['excluded'](name)


@pytest.mark.parametrize('location', ['source', 'public', 'symlink', 'broad-permissions'])
def test_replacement_rules_must_be_private_external_regular_file(tmp_path, repos, location):
    source, public, _ = repos
    path = ((source if location == 'source' else public) / 'private-rules.json'
            if location in {'source', 'public'} else tmp_path / 'unsafe-rules.json')
    if location == 'symlink':
        real = tmp_path / 'real-rules.json'
        real.write_text('{"schema_version": 1}'); real.chmod(0o600)
        path.symlink_to(real)
    else:
        path.write_text('{"schema_version": 1}')
        path.chmod(0o644 if location == 'broad-permissions' else 0o600)
    result, output, report = run_export(tmp_path, repos, extra=('--rules', str(path)))
    assert result.returncode == 1 and report['error'] == 'use_private_external_rules'
    assert not output.exists()
