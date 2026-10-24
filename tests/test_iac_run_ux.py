"""Run/Repositories UX: rescan runs are labeled as such, drill-in + rescan
is available from the run and repositories tables, and the latest-state view
reflects every finished run (not only fully succeeded ones)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.iac_service import IacService
from app.models.iac import IacScanRun

ROOT = Path(__file__).resolve().parents[1]


def test_rescan_run_is_labeled_with_the_repo_it_scans():
    run = IacScanRun(
        trigger="rescan",
        single_project_id=101,
        target_url="https://gitlab.enterprise.example.com/sandbox/portal/app",
        scope_path="sandbox/portal",
    )
    assert run.is_rescan
    assert run.target_label == "sandbox/portal/app"


def test_group_run_is_labeled_with_its_scope():
    run = IacScanRun(
        trigger="manual",
        single_project_id=None,
        target_url="https://gitlab.enterprise.example.com/sandbox/portal",
        scope_path="sandbox/portal",
    )
    assert not run.is_rescan
    assert run.target_label == "sandbox/portal"


def test_rescan_run_without_target_url_falls_back_to_scope():
    run = IacScanRun(trigger="rescan", single_project_id=5, target_url="", scope_path="sandbox/portal")
    assert run.target_label == "sandbox/portal"


def test_latest_repo_rows_include_partial_and_failed_runs():
    # Query construction needs no bind: check the compiled filter.
    service = IacService(Session())
    sql = str(
        service.latest_repo_rows_query().statement.compile(compile_kwargs={"literal_binds": True})
    )
    for status in ("succeeded", "partial", "failed"):
        assert f"'{status}'" in sql


def test_run_page_has_per_row_rescan_button():
    source = (ROOT / "app/templates/iac_run.html").read_text()
    assert '/rescan" style="margin:0;">' in source
    assert source.count("Worker has not discovered") == 1
    assert 'colspan="8"' in source


def test_iac_run_route_uses_focusable_xwide_responsive_table_contract():
    template = (ROOT / "app/templates/iac_run.html").read_text()
    router = (ROOT / "app/routers/web/iac.py").read_text()
    styles = (ROOT / "app/static/css/signal-river.css").read_text()

    assert '"/iac/runs/{run_id}"' in router
    assert '"iac_run.html"' in router
    assert "{% import 'macros/data_surfaces.html' as data_surfaces %}" in template
    assert "table-container has-responsive-table" in template
    assert (
        "data_surfaces.responsive_table("
        "'iac-run-repositories-table', 'iac-run-repositories-title', 'xwide'"
    ) in template
    assert 'id="iac-run-repositories-title"' in template
    assert 'aria-label="IaC run repository results"' in template
    assert ".responsive-table-scroll {" in styles
    assert "width: 100%;" in styles
    assert "max-width: 100%;" in styles
    assert "min-width: 0;" in styles
    assert "overflow-x: auto;" in styles
    assert ".responsive-table-scroll-xwide > .data-table {" in styles
    assert "min-width: 1420px;" in styles


def test_repositories_page_has_per_row_rescan_button():
    source = (ROOT / "app/templates/iac_repositories.html").read_text()
    assert 'formaction="/iac/repositories/{{ repo.id }}/rescan"' in source
    assert 'colspan="8"' in source


def test_dashboard_shows_scan_error_banner():
    source = (ROOT / "app/templates/iac.html").read_text()
    assert "{% if scan_error %}" in source


def test_runs_page_shows_trigger_and_target_label():
    source = (ROOT / "app/templates/iac_runs.html").read_text()
    assert "run.target_label" in source
    assert "run.is_rescan" in source
    assert 'colspan="10"' in source


def test_cancel_route_is_registered_and_run_pages_offer_it_for_active_runs():
    from app.routers.web.iac import router

    paths = {getattr(route, "path", None) for route in router.routes}
    assert "/iac/runs/{run_id}/cancel" in paths
    assert "/cancel" in (ROOT / "app/templates/iac_run.html").read_text()
    assert "/cancel" in (ROOT / "app/templates/iac_runs.html").read_text()
