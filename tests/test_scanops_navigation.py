from types import SimpleNamespace
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


def test_scanops_brand_uses_the_shared_signal_control_mark():
    env = Environment(loader=FileSystemLoader("app/templates"))
    template = env.from_string(
        "{% import 'macros/brand.html' as brand %}{{ brand.mark() }}"
    )

    mark = template.render()
    assert '<span class="scanops-mark"' in mark
    assert 'src="/static/scanops-mark.svg?v=20260814-scanops-mark-2"' in mark

    mark = (ROOT / "app/static/scanops-mark.svg").read_text()
    assert 'aria-label="ScanOps"' in mark
    assert "#b7f34a" in mark

    shared_asset = "/static/scanops-mark.svg?v=20260814-scanops-mark-2"
    assert shared_asset in (ROOT / "frontend/index.html").read_text()
    assert shared_asset in (ROOT / "frontend/src/shared.tsx").read_text()


def test_scanops_navigation_exposes_scanops_information_architecture():
    env = Environment(loader=FileSystemLoader("app/templates"))
    request = SimpleNamespace(url=SimpleNamespace(path="/iac"))

    html = env.get_template("base.html").render(
        request=request,
        ui_auth_enabled=True,
    )

    for section in (
        "Overview",
        "Coverage",
        "Executions",
        "Scanner fleet",
        "Products",
        "Repositories",
        "Integrations",
    ):
        assert section in html

    assert 'href="/scanners"' in html
    assert 'href="/logs"' in html
    assert 'href="/iac" class="side-sublink active">IaC scanning</a>' in html
    assert 'aria-label="Execution views"' in html
    assert 'href="/failed"' not in html
    assert 'href="/reports" class="side-link' not in html
    assert 'signal-river.css' in html
    assert 'href="/static/scanops-mark.svg?v=20260814-scanops-mark-2"' in html
    assert 'class="scanops-mark"' in html
    assert "river-mark" not in html
    assert all(label in html for label in ("Operate", "Understand", "Platform"))
    assert all(helper not in html for helper in ("executions & recovery", "targets & controls", "scanners & delivery"))
    assert "ScanOps runtime status" in html
    assert "Last poll" in html
    assert "Live telemetry" not in html


def test_legacy_failed_jobs_route_stays_inside_executions_navigation():
    env = Environment(loader=FileSystemLoader("app/templates"))
    request = SimpleNamespace(url=SimpleNamespace(path="/failed"))

    html = env.get_template("base.html").render(request=request, ui_auth_enabled=True)

    assert 'href="/logs?status=failed" class="side-link side-link-danger active"' in html
    assert 'href="/logs" class="side-sublink active"' not in html
    assert 'href="/failed"' not in html
    assert "Incidents" not in html


def test_targets_and_fleet_routes_have_distinct_top_level_destinations():
    env = Environment(loader=FileSystemLoader("app/templates"))

    products = env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/targets/products/3")), ui_auth_enabled=True
    )
    repositories = env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/targets/repositories/7")), ui_auth_enabled=True
    )
    fleet = env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/scanners")), ui_auth_enabled=True
    )

    assert 'href="/targets?level=product" class="side-link active"' in products
    assert 'href="/targets?level=repository" class="side-link active"' in repositories
    assert 'href="/scanners" class="side-link active"' in fleet

    target_detail = env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/targets/repositories/7")), ui_auth_enabled=True
    )
    target_jobs = env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/repos/7/jobs")), ui_auth_enabled=True
    )
    assert 'href="/targets?level=repository" class="side-link active"' in target_detail
    assert 'href="/logs" class="side-sublink active">Job journal</a>' in target_jobs


def test_target_operator_modes_mark_the_matching_inventory_destination():
    env = Environment(loader=FileSystemLoader("app/templates"))

    products = env.get_template("base.html").render(
        request=SimpleNamespace(
            url=SimpleNamespace(path="/targets/operator"),
            query_params={"level": "product"},
        ),
        ui_auth_enabled=True,
    )
    repositories = env.get_template("base.html").render(
        request=SimpleNamespace(
            url=SimpleNamespace(path="/targets/operator"),
            query_params={"level": "repository"},
        ),
        ui_auth_enabled=True,
    )

    assert 'href="/targets?level=product" class="side-link active"' in products
    assert (
        'href="/targets?level=repository" class="side-link active"'
        in repositories
    )


def test_scanops_registers_scanner_and_integration_routes():
    from app.main import app

    def collect_paths(router):
        paths = set()
        for route in router.routes:
            if hasattr(route, "path"):
                paths.add(route.path)
            elif hasattr(route, "original_router"):
                paths.update(collect_paths(route.original_router))
        return paths

    paths = collect_paths(app.router)
    assert "/scanners" in paths
    assert "/scanners/operator" in paths
    assert "/integrations" in paths
    assert "/integrations/operator" in paths
    assert "/integrations/{key}/save" in paths
    assert "/integrations/{key}/test" in paths
    assert "/integrations/{key}/disable" in paths


def test_execution_navigation_is_owned_by_the_shared_page_shell():
    base = open("app/templates/base.html", encoding="utf-8").read()

    assert "execution_nav.switcher(" not in base
    assert 'aria-label="Execution views"' in base
    for href in ("/logs", "/pipelines", "/config-scan", "/iac", "/ptai-scans"):
        assert f'href="{href}" class="side-sublink' in base
    for template in ("logs.html", "pipelines.html", "config_scan.html", "iac.html", "ptai_scans.html"):
        source = open(f"app/templates/{template}", encoding="utf-8").read()
        assert "execution_source_tabs" not in source
        assert "execution_nav.switcher(" not in source


def test_execution_sidebar_has_five_canonical_addressable_modules():
    env = Environment(loader=FileSystemLoader("app/templates"))
    html = env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/iac")),
        ui_auth_enabled=True,
    )

    for href, label in (
        ("/logs", "Job journal"),
        ("/pipelines", "Active pipelines"),
        ("/config-scan", "Config Guard"),
        ("/iac", "IaC scanning"),
        ("/ptai-scans", "PT AI"),
    ):
        assert f'href="{href}" class="side-sublink' in html
        assert f">{label}</a>" in html
    assert 'href="/iac" class="side-sublink active">IaC scanning</a>' in html


def test_execution_sidebar_marks_the_current_module_from_its_url():
    env = Environment(loader=FileSystemLoader("app/templates"))

    for path, href, label in (
        ("/logs", "/logs", "Job journal"),
        ("/repos/7/jobs", "/logs", "Job journal"),
        ("/pipelines/42", "/pipelines", "Active pipelines"),
        ("/config-scan/products/3", "/config-scan", "Config Guard"),
        ("/iac/runs/8", "/iac", "IaC scanning"),
        ("/ptai-scans/9", "/ptai-scans", "PT AI"),
    ):
        html = env.get_template("base.html").render(
            request=SimpleNamespace(url=SimpleNamespace(path=path)),
            ui_auth_enabled=True,
        )
        assert f'href="{href}" class="side-sublink active">{label}</a>' in html


def test_obsolete_execution_switcher_assets_are_removed():
    styles = (ROOT / "app/static/css/signal-river.css").read_text(encoding="utf-8")

    assert ".execution-module-switcher" not in styles
    assert not (ROOT / "app/templates/macros/execution_navigation.html").exists()


def test_operator_shell_keeps_truthful_persistent_sidebar_collapse():
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    react_shell = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "app/static/css/signal-control.css").read_text(
        encoding="utf-8"
    )
    javascript = (ROOT / "app/static/js/main.js").read_text(encoding="utf-8")

    assert 'id="sidebar-toggle"' in base
    assert "secpipe.sidebar.collapsed" in base
    assert "function initSidebarToggle()" in javascript
    assert ".signal-river-layout.sidebar-collapsed .side-nav" in styles
    assert "ScanOps Operator" not in base
    assert "Basic-auth session" not in base
    assert "ScanOps Operator" not in react_shell
    assert "Basic-auth session" not in react_shell


def test_legacy_logs_source_urls_redirect_to_canonical_routes():
    source = open("app/routers/web/logs.py", encoding="utf-8").read()

    assert 'source: str = "all"' in source
    assert '"pipelines": "/pipelines"' in source
    assert '"config-guard": "/config-scan"' in source
    assert '"checkov": "/iac"' in source
    assert '"ptai": "/ptai-scans"' in source
    assert "return RedirectResponse(url=source_destinations[source], status_code=303)" in source


def test_execution_source_tabs_partial_is_removed():
    assert not (ROOT / "app/templates/macros/execution_source_tabs.html").exists()
