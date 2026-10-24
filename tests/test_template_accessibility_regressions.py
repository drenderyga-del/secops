import re
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_config_scan_export_uses_scanops_brand_and_aa_severity_badges() -> None:
    source = (TEMPLATES / "config_scan_report_export.html").read_text()

    assert "Secpipe Monitor" not in source
    assert "ScanOps &mdash; GitLab Config Security Report" in source
    assert "@media print" in source

    badge_colors = {
        "critical": ("#8f1237", "#fff"),
        "high": ("#9d2b0b", "#fff"),
        "medium": ("#f6c344", "#241800"),
        "low": ("#3548b8", "#fff"),
        "info": ("#16613d", "#fff"),
    }
    for severity, (background, foreground) in badge_colors.items():
        rule = re.search(rf"\.{severity} \{{([^}}]+)\}}", source)
        assert rule, severity
        assert f"background: {background}" in rule.group(1)
        assert f"color: {foreground}" in rule.group(1)
        normalized_foreground = "#ffffff" if foreground == "#fff" else foreground
        assert _contrast_ratio(background, normalized_foreground) >= 4.5


def test_dynamic_audit_scan_exposes_live_and_progress_semantics() -> None:
    template = (TEMPLATES / "audit.html").read_text()
    javascript = (ROOT / "app" / "static" / "js" / "main.js").read_text()

    for contract in (
        'role="status"',
        'aria-live="polite"',
        'role="progressbar"',
        'aria-valuemin="0"',
        'aria-valuemax="100"',
        'aria-valuenow="{{ scan_progress }}"',
        "data-run-status=",
    ):
        assert contract in template

    assert "progressMeter.setAttribute('aria-valuenow', String(pct))" in javascript
    assert "progressMeter.setAttribute(" in javascript
    assert "'aria-valuetext'" in javascript
    assert "status: root.dataset.runStatus" in javascript


def test_server_sidebar_marks_exactly_one_current_destination() -> None:
    environment = Environment(loader=FileSystemLoader(TEMPLATES))
    template = environment.get_template("base.html")
    destinations = (
        ("/", {}, "/"),
        ("/logs", {}, "/logs"),
        ("/pipelines/42", {}, "/pipelines"),
        ("/coverage/products/3", {}, "/coverage"),
        ("/targets/operator", {"level": "repository"}, "/targets?level=repository"),
        ("/logs", {"status": "failed"}, "/logs?status=failed"),
        ("/integrations/gitlab", {}, "/integrations"),
    )

    for path, query, href in destinations:
        html = template.render(
            request=SimpleNamespace(
                url=SimpleNamespace(path=path), query_params=query
            ),
            ui_auth_enabled=True,
        )
        assert html.count('aria-current="page"') == 1, path
        assert re.search(
            rf'<a aria-current="page" href="{re.escape(href)}" class="side-(?:link|sublink)',
            html,
        ), path
