from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_config_scan_findings_project_query_is_optional():
    source = (ROOT / "app/routers/web/config_scan.py").read_text()
    assert 'project: str = Query("",' in source
    assert "ConfigScanFinding" in source


def test_iac_findings_group_queries_are_optional():
    source = (ROOT / "app/routers/web/iac.py").read_text()
    assert 'severity: str = Query("")' in source
    assert 'rule_id: str = Query("")' in source
    assert 'title: str = Query("")' in source


def test_iac_repositories_page_has_server_side_filter_controls():
    source = (ROOT / "app/templates/iac_repositories.html").read_text()
    assert 'class="filter-bar signal-filter-surface"' in source
    assert 'aria-label="Filter IaC repositories"' in source
    for field in ("q", "iac", "scan_status", "asoc_status", "has_important_findings"):
        assert f'name="{field}"' in source


def test_iac_findings_page_has_pagination_that_preserves_filters():
    source = (ROOT / "app/templates/iac_findings.html").read_text()
    route = (ROOT / "app/routers/web/iac.py").read_text()
    assert "Page {{ page }} / {{ total_pages }}" in source
    assert "{{ query_suffix }}" in source
    assert "page_size = 25" in route
    for field in ("severity", "rule", "project", "scanner"):
        assert f'name="{field}"' in source
    for fact in (
        "finding.severity",
        "finding.rule_id",
        "finding.title",
        "finding.scan_repository_id",
    ):
        assert fact in source


def test_iac_detail_and_config_guard_findings_keep_local_filters_in_history():
    iac_detail = (ROOT / "app/templates/iac_repository_detail.html").read_text()
    config_findings = (ROOT / "app/templates/config_scan_run_findings.html").read_text()

    for source, search_hook, severity_hook in (
        (iac_detail, "data-finding-search", "data-severity-filter"),
        (config_findings, "data-config-finding-search", "data-config-severity-filter"),
    ):
        assert 'class="filter-bar signal-filter-surface' in source
        assert search_hook in source
        assert severity_hook in source
        assert "window.history[push ? 'pushState' : 'replaceState']" in source
        assert "window.addEventListener('popstate'" in source


def test_iac_findings_is_an_explicit_iac_navigation_destination():
    tabs = (ROOT / "app/templates/iac_macros.html").read_text()
    route = (ROOT / "app/routers/web/iac.py").read_text()

    assert 'href="/iac/findings"' in tabs
    assert "active_tab == 'findings'" in tabs
    assert '"active_tab": "findings"' in route


def test_iac_active_run_pages_poll_status_without_full_page_reload():
    run_source = (ROOT / "app/templates/iac_run.html").read_text()
    dashboard_source = (ROOT / "app/templates/iac.html").read_text()
    assert "/iac/scan-status" in run_source
    assert "includes(payload.run.status)" in run_source
    assert run_source.count("window.location.reload") == 1
    assert "/iac/scan-status" in dashboard_source
    assert "includes(payload.run.status)" in dashboard_source
    assert dashboard_source.count("window.location.reload") == 1
