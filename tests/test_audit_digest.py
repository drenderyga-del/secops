"""Unit tests for the audit digest message builder."""
from types import SimpleNamespace

from app.audit_digest import MAX_REPOS_PER_GROUP, build_digest_text
from app.models.audit import AuditCoverageStatus
from app.target_navigation import COVERAGE_PATH


def _row(path: str, status: AuditCoverageStatus, web_url: str | None = None):
    return {
        "repository": SimpleNamespace(path_with_namespace=path, web_url=web_url),
        "snapshot": SimpleNamespace(status=status, reason=None),
        "lifecycle": None,
    }


def test_empty_rows_produce_no_digest():
    assert build_digest_text([]) is None


def test_digest_groups_by_status(monkeypatch):
    monkeypatch.setenv("SECPIPE_PUBLIC_URL", "http://monitor.local:8081")
    rows = [
        _row("group/repo-a", AuditCoverageStatus.NO_PIPELINES, "https://gitlab/group/repo-a"),
        _row("group/repo-b", AuditCoverageStatus.PIPELINE_NO_SEC),
        _row("group/repo-c", AuditCoverageStatus.NO_PIPELINES),
    ]

    text = build_digest_text(rows)

    assert "3 repositories requiring attention" in text
    assert "Pipelines are not running — 2" in text
    assert "Security job is missing — 1" in text
    assert "[group/repo-a](https://gitlab/group/repo-a)" in text  # linked
    assert "- group/repo-b" in text  # plain, no web_url
    assert f"http://monitor.local:8081{COVERAGE_PATH}" in text


def test_digest_notification_uses_canonical_coverage_link(monkeypatch):
    monkeypatch.setenv("SECPIPE_PUBLIC_URL", "https://scanops.example/")

    text = build_digest_text(
        [_row("group/repo-a", AuditCoverageStatus.NO_PIPELINES)]
    )

    assert f"Full list: https://scanops.example{COVERAGE_PATH}" in text
    assert "https://scanops.example/audit" not in text


def test_digest_truncates_long_groups():
    rows = [
        _row(f"g/repo-{i}", AuditCoverageStatus.NO_PIPELINES)
        for i in range(MAX_REPOS_PER_GROUP + 5)
    ]

    text = build_digest_text(rows)

    assert "and 5 more" in text
