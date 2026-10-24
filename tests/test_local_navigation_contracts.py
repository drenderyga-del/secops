from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"

FORMER_TOC_PAGES = (
    "audit.html",
    "audit_product_detail.html",
    "config_scan_product.html",
    "config_scan_run.html",
    "iac.html",
    "iac_repository_detail.html",
    "iac_run.html",
    "logs.html",
    "repository_360.html",
)


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_short_operational_pages_omit_redundant_section_navigation():
    for template_name in FORMER_TOC_PAGES:
        source = (TEMPLATES / template_name).read_text(encoding="utf-8")
        assert "macros/local_navigation.html" not in source, template_name
        assert "local_nav.toc(" not in source, template_name
        assert 'class="local-toc"' not in source, template_name
        assert 'class="section-tabs"' not in source, template_name

    assert not (TEMPLATES / "macros" / "local_navigation.html").exists()


def test_retired_section_navigation_has_no_css_or_javascript_runtime():
    styles = _source("app/static/css/style.css") + _source(
        "app/static/css/signal-river.css"
    )
    javascript = _source("app/static/js/main.js")

    for retired_token in (
        ".local-toc",
        ".local-toc-link",
        "data-local-toc",
        "setupLocalNavigation",
    ):
        assert retired_token not in styles
        assert retired_token not in javascript


def test_content_section_ids_remain_available_for_links_and_accessibility():
    target = _source("app/templates/repository_360.html")
    assert '<section id="posture" class="page-section target360-posture"' in target
    assert (
        '<section id="controls" class="page-section target360-section" '
        'aria-label="Repository security control coverage"'
    ) in target
    assert '<section id="timeline" class="page-section target360-section"' in target

    iac = _source("app/templates/iac.html")
    for section_id in ("scan", "metrics", "problems", "repositories"):
        assert f'id="{section_id}"' in iac


def test_real_route_switchers_remain_after_local_navigation_removal():
    for template_name in ("audit.html", "coverage_runs.html", "coverage_run_detail.html"):
        source = (TEMPLATES / template_name).read_text(encoding="utf-8")
        assert 'class="module-tabs" aria-label="Coverage views"' in source

    iac_tabs = _source("app/templates/iac_macros.html")
    assert iac_tabs.count('aria-current="page"') == 4
    assert 'class="module-tab' in iac_tabs
