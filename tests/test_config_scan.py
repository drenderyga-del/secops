"""Unit tests for config scan reporting helpers (no FastAPI app import)."""
from unittest.mock import MagicMock

from app.config_scan_reporting import (
    build_project_tree,
    findings_severity_counts,
    gitlab_path_from_product_field,
    resolve_project_findings_blob,
)
from app.config_scan_service import ConfigScanService


def test_findings_severity_counts():
    findings = [
        {"severity": "critical"},
        {"severity": "high"},
        {"severity": "high"},
        {"severity": "unknown_sev"},
    ]
    got = findings_severity_counts(findings)
    assert got["critical"] == 1
    assert got["high"] == 2
    assert got.get("unknown_sev", 0) == 1


def test_gitlab_path_from_product_field_url():
    assert (
        gitlab_path_from_product_field("https://gitlab.example.com/group/sub/repo")
        == "group/sub/repo"
    )


def test_build_project_tree_groups_under_scope():
    projects = [
        {
            "project_path": "acme/platform/api",
            "findings": [{"severity": "low"}],
            "project_url": "https://x/acme/platform/api",
        },
        {
            "project_path": "acme/platform/worker",
            "findings": [{"severity": "high"}, {"severity": "medium"}],
        },
    ]
    tree = build_project_tree(projects, "acme")
    assert tree["name"] == "."
    assert len(tree["children"]) == 1
    platform = tree["children"][0]
    assert platform["name"] == "platform"
    assert len(platform["repos"]) == 2
    names = {r["repo_name"] for r in platform["repos"]}
    assert names == {"api", "worker"}


def test_build_project_tree_keeps_repo_when_scope_equals_repo_path():
    projects = [
        {
            "project_path": "platform/infrastructure/asoc",
            "findings": [{"severity": "medium"}],
        },
        {
            "project_path": "platform/infrastructure/secpipe-monitor",
            "findings": [{"severity": "high"}],
        },
    ]
    tree = build_project_tree(projects, "platform/infrastructure/asoc")
    all_repo_names = set()

    def walk(node):
        for r in node.get("repos", []):
            all_repo_names.add(r["repo_name"])
        for child in node.get("children", []):
            walk(child)

    walk(tree)
    assert "asoc" in all_repo_names
    assert "secpipe-monitor" in all_repo_names


def test_resolve_project_findings_blob_prefers_report():
    run = MagicMock()
    run.id = 42
    report = {
        "projects": [
            {
                "project_path": "g/p",
                "findings": [{"severity": "critical", "title": "x"}],
            }
        ]
    }
    db = MagicMock()
    blob = resolve_project_findings_blob(db, run, report, "g/p")
    assert blob is not None
    assert blob["findings_summary"]["critical"] == 1
    db.query.assert_not_called()


def test_resolve_project_findings_blob_falls_back_to_db():
    run = MagicMock()
    run.id = 99
    report = {"projects": []}
    row = MagicMock(
        project_path="x/y",
        project_url="https://gitlab/x/y",
        severity="high",
        rule_id="r1",
        title="t",
        description=None,
        recommendation=None,
        detail=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
        row
    ]
    blob = resolve_project_findings_blob(db, run, report, "x/y")
    assert blob is not None
    assert blob["findings_summary"]["high"] == 1


def _mock_service_with_runs(runs):
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = runs
    return ConfigScanService(db)


def test_product_overview_bundle_keeps_repos_from_two_single_scans():
    run2 = MagicMock(id=2, status="succeeded")
    run1 = MagicMock(id=1, status="succeeded")
    svc = _mock_service_with_runs([run2, run1])
    product = MagicMock(id=1, path="platform/infrastructure", name="infra")

    svc.load_report_for_run = MagicMock(side_effect=[
        {"projects": [{"project_path": "platform/infrastructure/secpipe-monitor", "findings": [{"severity": "high"}]}]},
        {"projects": [{"project_path": "platform/infrastructure/asoc", "findings": [{"severity": "medium"}]}]},
    ])
    svc.get_findings_grouped_by_project = MagicMock(side_effect=[
        [{"project_path": "platform/infrastructure/secpipe-monitor", "project_url": None, "findings": [MagicMock(severity="high", rule_id="R1", title="t", description=None, recommendation=None, detail=None)], "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0}}],
        [{"project_path": "platform/infrastructure/asoc", "project_url": None, "findings": [MagicMock(severity="medium", rule_id="R2", title="t", description=None, recommendation=None, detail=None)], "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0}}],
    ])

    bundle = svc.build_product_overview_bundle(product)
    paths = {r["project_path"] for r in bundle["repo_table_rows"]}
    assert "platform/infrastructure/secpipe-monitor" in paths
    assert "platform/infrastructure/asoc" in paths
    assert len(paths) == 2


def test_product_overview_bundle_uses_latest_repo_state_for_remediation():
    run3 = MagicMock(id=3, status="succeeded")
    run2 = MagicMock(id=2, status="succeeded")
    svc = _mock_service_with_runs([run3, run2])
    product = MagicMock(id=1, path="platform/infrastructure", name="infra")

    repo_path = "platform/infrastructure/asoc"
    svc.load_report_for_run = MagicMock(side_effect=[
        {"projects": [{"project_path": repo_path, "findings": []}]},
        {"projects": [{"project_path": repo_path, "findings": [{"severity": "high"}, {"severity": "medium"}]}]},
    ])
    svc.get_findings_grouped_by_project = MagicMock(side_effect=[
        [],
        [{"project_path": repo_path, "project_url": None, "findings": [MagicMock(severity="high", rule_id="R1", title="t", description=None, recommendation=None, detail=None), MagicMock(severity="medium", rule_id="R2", title="t", description=None, recommendation=None, detail=None)], "summary": {"critical": 0, "high": 1, "medium": 1, "low": 0, "info": 0}}],
    ])

    bundle = svc.build_product_overview_bundle(product)
    row = next(r for r in bundle["repo_table_rows"] if r["project_path"] == repo_path)
    assert row["findings_run_id"] == 3
    assert row["findings_count"] == 0
    assert row["findings_summary"]["high"] == 0
    assert row["findings_summary"]["medium"] == 0


def test_product_overview_summary_counts_aggregated_rows():
    svc = ConfigScanService(MagicMock())
    bundle = {
        "repo_table_rows": [
            {
                "findings_count": 2,
                "findings_summary": {"critical": 1, "high": 1, "medium": 0, "low": 0, "info": 0},
            },
            {
                "findings_count": 0,
                "findings_summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
        ]
    }

    summary = svc.summarize_product_overview_bundle(bundle)

    assert summary["projects_scanned"] == 2
    assert summary["projects_total"] == 2
    assert summary["projects_with_findings"] == 1
    assert summary["findings_total"] == 2
    assert summary["severity"]["critical"] == 1
    assert summary["severity"]["high"] == 1
