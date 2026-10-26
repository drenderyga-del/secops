#!/usr/bin/env python3
"""Prepare a reviewed, history-free GitLab snapshot as a public three-way update.

The tool never edits either repository, commits, pushes or rewrites history.
Private replacement rules and reports belong outside both repositories.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile


EXCLUDED_DIRS = {
    '.git', '.gitlab', '.codex', '.agents', '.claude', '.impeccable', '.repowise',
    'cert', 'certs', 'node_modules', '__pycache__', '.venv', 'venv', 'dist',
    '.pytest_cache', '.playwright-cli', 'playwright-report', 'test-results',
}
EXCLUDED_NAMES = (
    '.gitlab*', 'CODEX_CONTEXT.md', '.codex*', 'codex-*', '*.local.*',
    '*.pem', '*.key', '*.p12', '*.pfx', '*.crt', '*.cer', '*.dump', '*.sql.gz',
    '*.db', '*.sqlite', '*.sqlite3', '*.pyc', '*.log',
)
EXCLUDED_PATHS = (
    'docs/superpowers/*', 'qa/artifacts/*', 'output/*',
    'scripts/gitlab_mr_gate.py', 'tests/test_gitlab_mr_gate.py',
)
PUBLIC_OWNED = ('README.md', 'AGENTS.md', 'CONTRIBUTING.md', 'LICENSE', '.github/*')


class ExportBlocked(Exception):
    pass


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.check_output(['git', '-C', str(repo), *args], input=input_bytes, stderr=subprocess.PIPE)


def revision(repo: Path, ref: str) -> str:
    # --end-of-options prevents a user-supplied ref from becoming a Git option.
    return git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or '..' in path.parts
            or str(path) != value or '\\' in value
            or any(ord(char) < 32 for char in value)):
        raise ExportBlocked('unsafe_path')
    return value


def excluded(path: str, patterns=()) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (any(part in EXCLUDED_DIRS for part in parts)
            or any(fnmatch.fnmatchcase(part.lower(), pattern.lower())
                   for part in parts for pattern in EXCLUDED_NAMES)
            or (name.startswith('.env') and name != '.env.example')
            or any(fnmatch.fnmatchcase(path, pattern)
                   for pattern in (*EXCLUDED_PATHS, *patterns)))


def tree(repo: Path, ref: str, exclusions=()) -> tuple[dict, list[str]]:
    records = []; omitted = []
    for raw in git(repo, 'ls-tree', '-rz', '--full-tree', ref).split(b'\0'):
        if not raw:
            continue
        metadata, raw_path = raw.split(b'\t', 1)
        path = safe_path(raw_path.decode('utf-8'))
        if excluded(path, exclusions):
            omitted.append(path)
            continue
        mode, kind, oid = metadata.decode().split()
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ExportBlocked('symlink_or_submodule')
        records.append((path, mode, oid))
    # One batch process avoids launching Git once per source file.
    payload = git(repo, 'cat-file', '--batch', input_bytes=''.join(oid + '\n' for _, _, oid in records).encode())
    entries = {}; offset = 0
    for path, mode, oid in records:
        end = payload.index(b'\n', offset)
        object_id, kind, size = payload[offset:end].decode().split()
        if object_id != oid or kind != 'blob':
            raise ExportBlocked('git_object_unavailable')
        start = end + 1; finish = start + int(size)
        if payload[finish:finish + 1] != b'\n':
            raise ExportBlocked('git_object_unavailable')
        entries[path] = (payload[start:finish], mode)
        offset = finish + 1
    if offset != len(payload):
        raise ExportBlocked('git_object_unavailable')
    return entries, omitted


def text(data: bytes) -> str | None:
    try:
        value = data.decode('utf-8')
    except UnicodeError:
        return None
    return None if '\0' in value else value


def transform(entries: dict, rules: list[dict], counts: dict) -> dict:
    result = {}
    for path, (data, mode) in entries.items():
        value = text(data)
        if value is not None:
            for rule in rules:
                if any(fnmatch.fnmatchcase(path, p) for p in rule.get('paths', ['*'])):
                    value, count = re.subn(re.escape(rule['find']), lambda _: rule['replace'],
                                          value, flags=re.I if rule.get('ignore_case', True) else 0)
                    counts[rule['id']] = counts.get(rule['id'], 0) + count
            data = value.encode('utf-8')
        result[path] = (data, mode)
    return result


def write_tree(directory: Path, entries: dict) -> None:
    for path, (data, mode) in entries.items():
        target = directory / safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode == '100755' else 0o644)


def scan(directory: Path, config: bytes, temporary: Path) -> None:
    if not shutil.which('gitleaks'):
        raise ExportBlocked('gitleaks_unavailable')
    config_path = temporary / 'gitleaks.toml'
    config_path.write_bytes(config)
    result = subprocess.run(['gitleaks', 'dir', '--redact=100', '--no-banner',
                             '--config', str(config_path), str(directory)],
                            capture_output=True)
    # Scanner output is deliberately not copied: reports must not echo matches.
    if result.returncode:
        raise ExportBlocked('secret_scan_failed' if result.returncode == 1 else 'secret_scan_unavailable')


def merge_text(current: bytes, base: bytes, incoming: bytes, temporary: Path) -> bytes | None:
    files = [temporary / name for name in ('public', 'base', 'incoming')]
    for path, data in zip(files, (current, base, incoming)):
        path.write_bytes(data)
    result = subprocess.run(['git', 'merge-file', '-p', *map(str, files)], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def validate_config(config: dict) -> None:
    allowed = {'schema_version', 'exclude', 'keep_public', 'replacements', 'forbidden_literals', 'approved_binary_sha256'}
    if not isinstance(config, dict) or set(config) - allowed or config.get('schema_version') != 1:
        raise ExportBlocked('invalid_rules')
    seen = set()
    for rule in config.get('replacements', []):
        if (not isinstance(rule, dict) or set(rule) - {'id', 'find', 'replace', 'paths', 'ignore_case'}
                or not isinstance(rule.get('id'), str) or not rule['id'] or rule['id'] in seen
                or not isinstance(rule.get('find'), str) or not rule['find']
                or not isinstance(rule.get('replace'), str)
                or not isinstance(rule.get('ignore_case', True), bool)
                or not isinstance(rule.get('paths', ['*']), list)
                or not all(isinstance(p, str) for p in rule.get('paths', ['*']))):
            raise ExportBlocked('invalid_rules')
        seen.add(rule['id'])
    for key in ('exclude', 'keep_public', 'forbidden_literals', 'approved_binary_sha256'):
        if not isinstance(config.get(key, []), list) or not all(isinstance(v, str) and v for v in config.get(key, [])):
            raise ExportBlocked('invalid_rules')
    if any(not re.fullmatch('[0-9a-f]{64}', v) for v in config.get('approved_binary_sha256', [])):
        raise ExportBlocked('invalid_rules')


def prepare(source: Path, base_ref: str, source_ref: str, public: Path,
            output: Path, rules_path: Path, report: dict) -> None:
    config = json.loads(rules_path.read_text())
    validate_config(config)
    base_sha = revision(source, base_ref); source_sha = revision(source, source_ref)
    public_sha = revision(public, 'HEAD')
    if subprocess.run(['git', '-C', str(source), 'merge-base', '--is-ancestor', base_sha, source_sha],
                      capture_output=True).returncode:
        raise ExportBlocked('source_base_not_ancestor')
    report.update(source_base=base_sha, source_revision=source_sha, public_revision=public_sha)
    base_raw, _ = tree(source, base_sha, config.get('exclude', []))
    incoming_raw, omissions = tree(source, source_sha, config.get('exclude', []))
    current, public_omissions = tree(public, public_sha)
    if public_omissions:
        raise ExportBlocked('public_tree_contains_local_artifacts')
    report['excluded_paths'] = omissions
    scanner_config = current.get('.gitleaks.toml')
    if scanner_config is None:
        raise ExportBlocked('public_scanner_config_missing')
    with tempfile.TemporaryDirectory(prefix='.public-export-', dir=output.parent) as tmp:
        temporary = Path(tmp); temporary.chmod(0o700)
        raw_dir = temporary / 'raw'; raw_dir.mkdir()
        write_tree(raw_dir, incoming_raw)
        scan(raw_dir, scanner_config[0], temporary)
        base = transform(base_raw, config.get('replacements', []), {})
        counts = {}; incoming = transform(incoming_raw, config.get('replacements', []), counts)
        report['replacement_counts'] = counts
        result = dict(current); updated = []; removed = []; kept = []; conflicts = []
        for path in sorted(set(base) | set(incoming)):
            old = base.get(path); new = incoming.get(path); present = current.get(path)
            if old == new:
                continue
            if any(fnmatch.fnmatchcase(path, p) for p in (*PUBLIC_OWNED, *config.get('keep_public', []))):
                kept.append(path)
                continue
            if present == new:
                continue
            if present == old:
                if new is None:
                    result.pop(path, None); removed.append(path)
                else:
                    result[path] = new; updated.append(path)
                continue
            if old and new and present and all(text(v[0]) is not None for v in (old, new, present)):
                merged = merge_text(present[0], old[0], new[0], temporary)
                # Independently changed executable modes also need review.
                mode_conflict = present[1] != old[1] and new[1] != old[1] and present[1] != new[1]
                if merged is not None and not mode_conflict:
                    result[path] = (merged, new[1] if present[1] == old[1] else present[1])
                    updated.append(path)
                    continue
            conflicts.append(path)
        report.update(updated_paths=updated, removed_paths=removed,
                      preserved_public_paths=kept, conflicts=conflicts)
        if conflicts:
            raise ExportBlocked('content_conflicts')
        approved = {hashlib.sha256(value[0]).hexdigest() for value in current.values() if text(value[0]) is None}
        approved.update(config.get('approved_binary_sha256', []))
        unapproved = []; residual = []; nonenglish = []
        for path, (data, _) in result.items():
            value = text(data)
            if value is None and hashlib.sha256(data).hexdigest() not in approved:
                unapproved.append(path)
            if value is not None and path.endswith('.md') and re.search('[\u0400-\u04ff]', value):
                nonenglish.append(path)
            haystack = path.casefold() + '\n' + (value.casefold() if value is not None else '')
            if any(marker.casefold() in haystack for marker in config.get('forbidden_literals', [])):
                residual.append(path)
        report.update(unapproved_binary_paths=unapproved, residual_marker_paths=residual,
                      nonenglish_markdown_paths=nonenglish)
        if unapproved or residual or nonenglish:
            raise ExportBlocked('publication_review_required')
        staged = temporary / 'snapshot'; staged.mkdir()
        write_tree(staged, result)
        scan(staged, scanner_config[0], temporary)
        # Exclusive mkdir prevents replacing another export. Either repository is untouched.
        output.mkdir(mode=0o700)
        write_tree(output, result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--base-ref', required=True)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--public', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rules', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = {'schema_version': 1, 'status': 'blocked'}
    external_paths_valid = False
    try:
        args.source = args.source.resolve(); args.public = args.public.resolve()
        args.output = args.output.resolve(); args.report = args.report.resolve()
        if (args.output.exists() or args.report.exists()
                or any(path.is_relative_to(repo) for path in (args.output, args.report)
                       for repo in (args.source, args.public))):
            raise ExportBlocked('use_new_external_output_and_report')
        if not args.output.parent.is_dir() or not args.report.parent.is_dir():
            raise ExportBlocked('external_parent_missing')
        external_paths_valid = True
        prepare(args.source, args.base_ref, args.ref, args.public, args.output, args.rules, report)
        report['status'] = 'prepared_for_review'
    except ExportBlocked as error:
        report['error'] = str(error)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        report['error'] = 'export_failed'
    # Exclusive creation prevents overwriting an earlier private audit report.
    try:
        if not external_paths_valid:
            print(json.dumps({'status': 'blocked', 'error': report.get('error')}))
            return 1
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2); handle.write('\n')
    except OSError:
        print(json.dumps({'status': 'blocked', 'error': 'report_not_written'}))
        return 1
    print(json.dumps({'status': report['status'], 'error': report.get('error'), 'report': str(args.report)}))
    return 0 if report['status'] == 'prepared_for_review' else 1


if __name__ == '__main__':
    raise SystemExit(main())
