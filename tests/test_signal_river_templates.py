import re
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.routers.web.common import _format_local_datetime
from app.routers.web.dashboard import _dashboard_signal
from app.target_navigation import COVERAGE_PATH, targets_url


def _env():
    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    env.filters["duration"] = lambda value: str(value or "")
    env.filters["urlencode"] = lambda value: str(value or "")
    env.globals.update(coverage_url=COVERAGE_PATH, targets_url=targets_url)
    return env


def test_local_datetime_filter_formats_iso_payloads_for_the_ui():
    assert _format_local_datetime("2026-07-21T14:19:52+00:00", "%Y-%m-%d %H:%M") == "2026-07-21 17:19"


def test_dashboard_topology_keeps_signal_river_and_live_metrics():
    html = _env().get_template("dashboard.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/")),
        ui_auth_enabled=True,
        stats={
            "total_repositories": 12,
            "total_products": 4,
            "total_jobs": 216,
            "success_rate": 90.0,
            "failed_jobs_count": 13,
        },
        active_pipeline_count=3,
        topology={
            "observed_steps": 216,
            "target_count": 12,
            "gitlab_triggers": 9,
            "active_pipelines": 3,
            "success_rate": 75.0,
            "failure_rate": 25.0,
            "execution_completed": 9,
            "execution_failed": 3,
            "execution_total": 12,
            "execution_timeframe": "7d",
            "execution_timeframe_label": "Last 7 days",
            "normalized_outputs": 216,
            "failed_outputs": 13,
            "config_drift": 15,
            "asoc_mapped_products": 3,
            "total_products": 4,
            "last_execution_at": "2026-07-20T10:00:00+00:00",
            "last_execution_label": "1d ago",
            "telemetry_state": "live",
        },
        chart_timeframes=[
            {"value": "1d", "label": "24 hours"},
            {"value": "7d", "label": "7 days"},
        ],
        chart_tools=[
            {"value": "all", "label": "All jobs"},
            {"value": "sast", "label": "SAST"},
        ],
        selected_timeframe="7d",
        selected_tool="all",
        audit_compact={"secpipe_working": 8, "total": 12, "secpipe_missing": 2, "secpipe_present_not_working": 2},
        config_scan_compact={"open_findings": 15, "products_scanned": 3},
        iac_compact={"critical": 2, "high": 3, "scanned_repositories": 7},
        pipeline_duration={
            "avg_label": "12m",
            "median_label": "11m",
            "p95_label": "18m",
            "p95_tail_label": "7 min",
            "count": 12,
        },
        job_duration={"avg_label": "2m"},
        job_duration_breakdown=[],
        chart_summary={"total": 216, "successful": 203, "failed": 13, "failure_rate": 6.0},
        chart_data={"labels": [], "display_labels": [], "success": [], "failed": []},
        overview_signal={
            "tone": "problem",
            "title": "13 failed jobs need operator context.",
            "context": "13 failed jobs · 15 configuration findings · 5 critical/high IaC findings",
            "action_label": "Inspect failed jobs",
            "action_href": "/logs?status=failed&timeframe=7d",
        },
    )

    assert "Execution river" in html
    assert "Everything is moving." in html
    assert "13 failed jobs need operator context." in html
    assert 'data-overview-signal' in html
    assert 'class="overview-action-link"' in html
    assert "216</b> observed steps" in html
    assert "GitLab pipelines" in html
    assert "9</b> pipelines · <b data-topology-metric=\"target_count\">12</b> targets" in html
    assert "Worker queue" in html
    assert "3</b> active pipelines" in html
    assert "Security controls" in html
    assert "3</b> failed · <b data-topology-metric=\"execution_total\">12</b> terminal" in html
    assert ">attention</span>" in html
    assert 'data-river-stage="source"' in html
    assert 'data-river-stage="queue"' in html
    assert 'data-river-stage="controls"' in html
    assert 'data-river-stage="evidence"' in html
    assert 'data-river-stage="delivery"' in html
    assert "river-stage-problem" in html
    assert 'class="topology-outcome-grid"' in html
    assert 'class="topology-outcome topology-outcome-success"' in html
    assert 'href="/pipelines?status=completed&amp;timeframe=7d"' in html
    assert 'data-topology-metric="success_rate">75.0</b><small>%</small>' in html
    assert "9</b> completed of <b data-topology-metric=\"execution_total\">12</b> terminal pipelines · Last 7 days" in html
    assert 'class="topology-outcome topology-outcome-failed"' in html
    assert 'class="topology-outcome topology-outcome-neutral"' in html
    assert 'class="topology-outcome topology-outcome-delivery"' in html
    assert 'href="/pipelines?status=failed&amp;timeframe=7d"' in html
    assert 'data-topology-metric="failure_rate">25.0</b><small>%</small>' in html
    assert "Pipeline failures" in html
    assert "3</b> failed of <b data-topology-metric=\"execution_total\">12</b> terminal pipelines · Last 7 days" in html
    assert "Failed output" not in html
    assert "Open failed jobs" not in html
    assert "Normalized evidence" in html
    assert "216</b> records" in html
    assert "3</b> / <b data-topology-metric=\"total_products\">4</b> products" in html
    assert "Failed jobs" in html
    assert "3 failed pipelines · signal 1d ago" in html
    assert "Across 3 scanned products" in html
    assert "Across 7 scanned repositories" in html
    assert "Across 12 observed targets" in html
    assert 'data-topology-url="/dashboard/topology?timeframe=7d"' in html
    assert 'class="river-output-grid"' not in html
    assert "Active control plane" in html
    assert 'data-topology-last-execution' in html
    assert ">1d ago</time>" in html
    assert 'aria-label="Security tool"' in html
    assert 'id="overview-timeframe"' in html
    assert 'id="chart-timeframe"' not in html
    assert html.count('name="timeframe" data-auto-submit="true"') == 1
    assert 'type="hidden" name="timeframe" value="7d"' in html
    assert 'name="timeframe" data-auto-submit="true"' in html
    assert 'id="chart-tool"' in html
    assert "Execution volume" in html
    assert "Current interval" in html
    assert 'class="latency-orbit"' in html
    assert "median flow" in html
    assert "12</b> terminal pipelines" in html
    assert "P95 tail <b>+7 min</b>" in html


def test_signal_control_overview_is_canonical_and_keeps_iac_drilldowns_reachable():
    source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert 'href="/overview/operator' not in source
    assert 'href={`/pipelines?status=failed&timeframe=${timeframe}`}' in source
    assert 'ariaLabel="Overview timeframe"' in source
    assert "data.attention.iac_critical_high" in source
    assert "<strong>IaC critical / high</strong>" in source
    assert 'secondaryHref="/iac/repositories"' in source


def test_signal_control_overview_keeps_operator_drilldowns_and_dense_context():
    source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert 'aria-label="Next operator action"' in source
    assert "Failure hotspots" in source
    assert "Posture snapshot" in source
    assert "Platform health" in source
    assert 'href={`/logs?status=failed&timeframe=${timeframe}`}' in source
    assert '`/pipelines?group=in_progress&timeframe=${timeframe}`' in source
    assert '`/logs?timeframe=${timeframe}`' in source
    assert "sample_count" in source
    assert "p95_tail_label" in source
    assert "Pipeline duration" in source
    assert "Open details" in source
    assert "Average pipeline" in source
    assert '"tracked · all time"' not in source
    assert 'Telemetry live' in source


def test_dashboard_signal_explains_stale_data_without_claiming_live_activity():
    signal = _dashboard_signal(
        topology={"telemetry_state": "stale", "last_execution_label": "5d ago"},
        stats={"failed_jobs_count": 17},
        config_scan={"open_findings": 8},
        iac={"critical": 1, "high": 3},
        audit={"secpipe_missing": 4, "secpipe_present_not_working": 3},
        timeframe="7d",
    )

    assert signal == {
        "tone": "stale",
        "title": "Latest execution signal arrived 5d ago.",
        "context": (
            "Showing last known state · 17 failed jobs · 8 configuration findings"
            " · 4 critical/high IaC findings · 7 coverage gaps"
        ),
        "action_label": "Inspect failed jobs",
        "action_href": "/logs?status=failed&timeframe=7d",
    }


def test_signal_control_shell_exposes_a_real_keyboard_command_palette():
    html = _env().get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/")),
        ui_auth_enabled=True,
    )
    javascript = Path("app/static/js/main.js").read_text(encoding="utf-8")
    styles = Path("app/static/css/signal-control.css").read_text(encoding="utf-8")

    assert 'id="command-palette-trigger"' in html
    assert 'aria-label="Open quick navigation"' in html
    assert 'aria-controls="command-palette"' in html
    assert '<dialog class="command-palette" id="command-palette"' in html
    assert 'id="command-palette-search"' in html
    for href in (
        "/",
        "/logs",
        "/pipelines",
        "/config-scan",
        "/iac",
        "/ptai-scans",
        "/coverage",
        "/targets?level=product",
        "/targets?level=repository",
        "/scanners",
        "/integrations",
    ):
        assert f'href="{href}"' in html

    assert "function initCommandPalette()" in javascript
    assert "event.key.toLowerCase() === 'k'" in javascript
    assert "event.key === 'Escape' && dialog.open" in javascript
    assert "dialog.addEventListener('cancel'" in javascript
    assert "dialog.addEventListener('close'" in javascript
    assert "trigger.focus();" in javascript
    assert ".command-palette::backdrop" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "body.signal-river-layout {" in styles
    assert "overflow-x: clip;" in styles
    assert ".signal-river-layout .responsive-table-scroll {" in styles
    assert "contain: layout paint inline-size;" in styles


def test_signal_control_overview_uses_shared_tokens_and_state_motion_only():
    styles = Path("app/static/css/signal-control.css").read_text(encoding="utf-8")

    assert "--bg-primary:" not in styles
    assert "--control-height:" not in styles
    assert "var(--control-height)" in styles
    assert "font-size: 0.6875rem" not in styles
    assert "font-size: 0.625rem" not in styles
    assert ".overview-live-state-stale" in styles
    assert ".overview-live-state-empty" in styles
    assert "@keyframes control-live-pulse" in styles
    assert "@keyframes control-orbit-dot" in styles
    assert ".river-stage-stale .river-node" in styles
    assert ".river-topology-panel.is-stale::before" in styles
    assert ".signal-chart-surface canvas" in styles
    chart_canvas_rule = styles.split(".signal-dashboard .signal-chart-surface canvas {", 1)[1].split("}", 1)[0]
    assert "height: 100%;" in chart_canvas_rule
    assert "min-height: 0;" in chart_canvas_rule
    reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".overview-live-state > i" in reduced_motion
    assert ".latency-orbit > i" in reduced_motion


def test_dashboard_gives_topology_and_success_rate_clear_visual_priority():
    source = open("app/static/css/signal-river.css", encoding="utf-8").read()

    assert ".topology-outcome-grid" in source
    assert ".topology-outcome-failed" in source
    assert "font-size: clamp(2.5rem, 5vw, 4.25rem);" in source
    assert "font-size: clamp(2.5rem, 5cqi, 4.25rem);" in source
    assert "min-height: 680px" in source
    primary_node_rule = source.split(".river-stage-primary .river-node {")[1].split("}", 1)[0]
    node_rule = source.split(".river-node {")[1].split("}", 1)[0]
    assert "position: absolute" in node_rule
    assert "top: 178.5px" in node_rule
    assert "transform: translate(-50%, -50%)" in node_rule
    assert "translateY" not in primary_node_rule
    assert ".execution-river { min-width: 780px; }" not in source
    assert ".execution-river .river-output-grid { grid-template-columns: 1fr;" in source


def test_dashboard_outcome_context_uses_a_full_row_instead_of_overflowing():
    source = open("app/static/css/signal-river.css", encoding="utf-8").read()
    base = open("app/templates/base.html", encoding="utf-8").read()

    assert "grid-template-columns: minmax(0, 1fr) auto;" in source
    assert ".topology-outcome-context {\n    grid-column: 1 / -1;" in source
    assert ".topology-outcome-grid {\n    container-type: inline-size;" in source
    assert "font-size: clamp(2.5rem, 5cqi, 4.25rem);" in source
    assert "grid-template-columns: minmax(130px, auto) auto minmax(160px, 1fr);" not in source
    assert "tokens.css?v=20260727-signal-control-palette-1" in base
    assert "signal-river.css?v=20260923-asoc-product-lifecycle-1" in base
    assert "signal-control.css?v=20260923-asoc-product-lifecycle-1" in base


def test_overview_keeps_its_operational_rail_until_the_narrow_layout():
    styles = Path("app/static/css/signal-control.css").read_text(encoding="utf-8")

    command_grid = styles.split(".signal-dashboard .river-command-grid {", 1)[1].split("}", 1)[0]
    telemetry_grid = styles.split(".signal-dashboard .signal-telemetry-grid {", 1)[1].split("}", 1)[0]
    narrow_layout = styles.split("@media (max-width: 720px)", 1)[1]

    assert "grid-template-columns: minmax(0, 1fr) var(--overview-rail);" in command_grid
    assert "grid-template-columns: minmax(0, 1fr) var(--overview-rail);" in telemetry_grid
    assert "@media (max-width: 1180px)" not in styles
    assert ".signal-dashboard .river-command-grid," in narrow_layout
    assert "grid-template-columns: 1fr;" in narrow_layout
    assert "display: flex;" in narrow_layout
    assert "flex-direction: column;" in narrow_layout


def test_sidebar_execution_drilldowns_are_aligned_and_have_a_clear_active_state():
    styles = open("app/static/css/signal-river.css", encoding="utf-8").read()
    tokens = open("app/static/css/tokens.css", encoding="utf-8").read()

    assert ".signal-river-layout .side-sublink {" in styles
    assert "--type-ui-nav: var(--type-action);" in tokens
    assert "min-height: 34px;" in styles
    assert "font-size: var(--type-label);" in styles
    assert ".signal-river-layout .side-sublink.active {" in styles
    assert "font-weight: 600;" in styles
    assert "text-align: left;" in styles
    assert "font-family: inherit;" in styles
    assert ".signal-river-layout .side-sublink.active::after" in styles
    assert "inset 3px 0 0" not in styles


def test_sidebar_uses_deterministic_product_typography():
    styles = open("app/static/css/signal-river.css", encoding="utf-8").read()
    control_styles = open("app/static/css/signal-control.css", encoding="utf-8").read()
    tokens = open("app/static/css/tokens.css", encoding="utf-8").read()

    assert '--font-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", ui-sans-serif, system-ui, sans-serif;' in tokens
    assert "font-family: var(--font-ui);" in styles
    assert "font-family: Inter" not in styles
    assert ".signal-river-layout .side-group-title {" in control_styles
    assert "font-family: var(--font-mono);" in control_styles
    assert "font-size: var(--type-label);" in control_styles
    base = open("app/templates/base.html", encoding="utf-8").read()
    assert '<span class="side-group-title">Operate</span>' in base
    assert '<span class="side-group-title">Understand</span>' in base
    assert '<span class="side-group-title">Platform</span>' in base
    assert "side-link-icon" not in base
    assert "targets & controls" not in base


def test_redundant_page_local_navigation_is_fully_retired():
    styles = open("app/static/css/signal-river.css", encoding="utf-8").read()
    javascript = open("app/static/js/main.js", encoding="utf-8").read()

    assert ".local-toc" not in styles
    assert ".local-toc-link" not in styles
    assert "setupLocalNavigation" not in javascript
    assert "data-local-toc" not in javascript


def test_real_route_tabs_use_product_typography_and_accent_state():
    styles = open("app/static/css/signal-river.css", encoding="utf-8").read()
    tabs = open("app/templates/iac_macros.html", encoding="utf-8").read()

    assert ".signal-river-layout .module-tab.active {" in styles
    assert "font-family: inherit;" in styles
    assert "background: color-mix(in oklch, var(--accent-primary) 14%, var(--bg-secondary));" in styles
    assert tabs.count('aria-current="page"') == 4


def test_page_headers_group_title_and_description_as_one_layout_unit():
    for template_name in (
        "config_scan.html",
        "iac.html",
        "products.html",
        "pipelines.html",
        "repositories.html",
        "ptai_scans.html",
    ):
        source = Path("app/templates", template_name).read_text(encoding="utf-8")
        assert re.search(r'<div class="page-header[^>]*">\s*<div>\s*<h1', source)


def test_product_maintenance_actions_are_not_primary_header_actions():
    source = Path("app/templates/products.html").read_text(encoding="utf-8")

    assert '<details id="maintenance" class="page-section maintenance-panel">' in source
    assert "These actions are not part of the normal monitoring workflow." in source


def test_engine_pages_do_not_repeat_execution_source_navigation():
    for template_name in ("pipelines.html", "config_scan.html", "iac.html", "ptai_scans.html"):
        source = Path("app/templates", template_name).read_text(encoding="utf-8")
        assert "execution_source_tabs" not in source


def test_short_pages_do_not_render_fake_anchor_tabs():
    for template_name in (
        "pipelines.html",
        "config_scan.html",
        "iac.html",
        "ptai_scans.html",
        "products.html",
        "repositories.html",
        "product_repositories.html",
        "iac_runs.html",
        "iac_repositories.html",
        "iac_findings.html",
    ):
        source = Path("app/templates", template_name).read_text(encoding="utf-8")
        assert 'class="section-tabs"' not in source


def test_dashboard_topology_client_polls_without_reloading_the_page():
    source = open("app/static/js/main.js", encoding="utf-8").read()

    assert "initDashboardTopology();" in source
    assert "setInterval(pollTopology, 15000);" in source
    assert "fetch(panel.dataset.topologyUrl" in source
    assert "state.textContent = 'stale'" in source
    assert "setLastExecution(data.last_execution_at);" in source
    assert "syncRiverStageStates(data);" in source
    assert "syncOverviewSignal(data);" in source
    assert "data-river-stage" in source
    assert "'river-stage-active'" in source
    assert "'river-stage-problem'" in source
    assert "'river-stage-stale'" in source
    assert "telemetryState === 'stale'" in source
    assert "data.telemetry_state === 'stale'" in source


def test_dashboard_chart_uses_signal_control_palette_and_failure_baseline():
    source = Path("app/static/js/main.js").read_text(encoding="utf-8")
    tokens = Path("app/static/css/tokens.css").read_text(encoding="utf-8")

    assert "--chart-primary: var(--accent-secondary);" in tokens
    assert "--chart-current: var(--accent-primary);" in tokens
    assert "getPropertyValue('--chart-primary')" in source
    assert "getPropertyValue('--chart-current')" in source
    assert "const failedY = yFor(failedValue);" in source
    assert "ctx.fillRect(x, failedY, barWidth, failedHeight);" in source
    assert "rgba(255, 107, 74" not in source


def test_signal_selects_are_custom_and_filters_apply_on_selection():
    source = open("app/static/js/main.js", encoding="utf-8").read()
    styles = open("app/static/css/signal-river.css", encoding="utf-8").read()
    logs = open("app/templates/logs.html", encoding="utf-8").read()

    assert "initSignalSelects();" in source
    assert "signal-select-trigger" in source
    assert "signal-select-menu" in source
    assert "role', 'listbox'" in source
    assert "document.body.appendChild(menu)" in source
    assert "select.dispatchEvent(new Event('change', { bubbles: true }))" in source
    assert "submitFilterForm(container)" in source
    assert ".signal-select-trigger" in styles
    assert ".signal-select-menu" in styles
    assert 'data-auto-apply="true"' in logs
    assert 'id="timeframe" data-auto-submit="true"' in logs
    assert ">Apply</span></button>" not in logs


def test_signal_select_tab_moves_focus_from_portal_back_into_document_order():
    source = open("app/static/js/main.js", encoding="utf-8").read()

    assert "function focusAdjacentControl(direction)" in source
    assert "event.preventDefault();\n                    closeMenu();" in source
    assert "focusAdjacentControl(event.shiftKey ? -1 : 1);" in source
    assert "closeMenu({ restoreFocus: event.key === 'Escape' });" not in source


def test_operational_filters_use_compact_non_modal_controls():
    audit = open("app/templates/audit.html", encoding="utf-8").read()
    source = open("app/static/js/main.js", encoding="utf-8").read()
    styles = open("app/static/css/signal-river.css", encoding="utf-8").read()

    assert "audit-product-trigger-compact" in audit
    assert "Math.min(240," in source
    assert ".signal-select-trigger {" in styles
    trigger_rule = styles.split(".signal-select-trigger {")[1].split("}", 1)[0]
    option_rule = styles.split(".signal-select-option {")[1].split("}", 1)[0]
    assert "height: 32px" in trigger_rule
    assert "min-height: 32px" in trigger_rule
    assert "font-size: var(--type-body)" in trigger_rule
    assert "height: 32px" in option_rule
    assert "min-height: 32px" in option_rule
    assert "font-size: var(--type-body)" in option_rule
    assert "min-height: var(--control-height);" in styles
    assert "min-height: var(--control-height-touch);" in styles
    assert ".signal-select-menu {" in styles
    assert "backdrop-filter: none" in styles
    assert ".audit-product-trigger-compact" in styles
    assert "max-width: 360px" in styles
    assert "@media (max-width: 720px)" in styles


def test_audit_metric_filters_keep_clear_selected_and_hover_states():
    styles = open("app/static/css/signal-river.css", encoding="utf-8").read()
    active_rule = styles.split(".signal-river-layout .audit-metric.active {")[1].split("}", 1)[0]

    assert ".signal-river-layout .audit-metric.active" in styles
    assert "outline: none;" in active_rule
    assert "border-color: var(--border-strong);" in active_rule
    assert "background: var(--bg-tertiary);" in active_rule
    assert "var(--audit-metric-glow)" not in active_rule
    assert "border-left-width: 1px" not in styles
    assert "--audit-metric-color: var(--accent-primary);" in styles
    assert "--audit-metric-color: var(--success);" in styles
    assert "--audit-metric-color: var(--error);" in styles
    assert "--audit-metric-color: var(--warning);" in styles
    assert ".signal-river-layout .audit-metric:hover" in styles
    assert "translateY(-1px)" in styles
    assert ".signal-river-layout .audit-metric:focus-visible" in styles


def test_shared_table_search_is_accessible_and_does_not_duplicate_page_filters():
    source = Path("app/static/js/main.js").read_text(encoding="utf-8")

    assert "document.querySelector('.filter-bar, .scan-target-form')" in source
    assert "searchInput.type = 'search'" in source
    assert "searchInput.className = 'table-search'" in source
    assert "searchInput.setAttribute('aria-label'" in source
    assert "searchInput.style.cssText" not in source


def test_mobile_page_headers_do_not_inherit_desktop_flex_height():
    styles = Path("app/static/css/signal-river.css").read_text(encoding="utf-8")

    assert ".signal-river-layout .page-header > div:first-child" in styles
    assert "flex: 0 1 auto;" in styles


def test_removed_local_toc_does_not_leave_orphaned_layout_selectors():
    styles = open("app/static/css/style.css", encoding="utf-8").read()
    styles += open("app/static/css/signal-river.css", encoding="utf-8").read()

    assert ".local-toc" not in styles
    assert ".local-toc-link" not in styles
    assert ":has(+ .local-toc)" not in styles


def test_base_busts_cached_assets_after_local_navigation_removal():
    base = open("app/templates/base.html", encoding="utf-8").read()

    assert "/static/css/style.css?v=20260730-local-toc-removal-1" in base
    assert "/static/css/signal-river.css?v=20260923-asoc-product-lifecycle-1" in base
    assert "/static/css/signal-control.css?v=20260923-asoc-product-lifecycle-1" in base
    assert "/static/js/main.js?v=20260805-filter-surface-2" in base


def test_scanner_fleet_exposes_all_current_engine_drilldowns():
    html = _env().get_template("scanners.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/scanners/operator")),
        ui_auth_enabled=True,
        scanners=[
            {
                "name": "GitLab security jobs",
                "role": "Pipeline-native execution adapter",
                "description": "Observed from CI.",
                "connection": {
                    "state": "healthy",
                    "label": "Connected",
                    "reason": "Source: GitLab passed its latest connection test.",
                },
                "integration_href": "/integrations?integration=gitlab",
                "integration_action": "Manage GitLab connection",
                "latest": {
                    "state": "healthy",
                    "label": "Success",
                    "occurred_at": None,
                    "summary": "semgrep_scan reported success.",
                },
                "workload": [
                    {"label": "Observed · 7d", "value": 10},
                    {"label": "Failed · 7d", "value": 1},
                ],
                "href": "/logs",
                "action": "Open executions",
            }
        ],
        ready_connections=1,
        running_executions=0,
        active_pipelines=1,
        failed_steps=1,
        last_activity=None,
    )

    assert "Security execution runtime" in html
    assert "GitLab security jobs" in html
    assert "Connection" in html
    assert "Latest run" in html
    assert "Workload window" in html
    assert 'href="/logs?status=failed"' in html
    assert 'href="/logs"' in html
    assert 'href="/integrations?integration=gitlab"' in html
    assert 'class="scanner-lane-action" data-action="drilldown"' in html
    assert "Manage GitLab connection" in html


def test_scanner_fleet_does_not_duplicate_integration_configuration():
    html = _env().get_template("scanners.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/scanners/operator")),
        ui_auth_enabled=True,
        scanners=[],
        ready_connections=0,
        running_executions=0,
        active_pipelines=0,
        failed_steps=0,
    )

    assert "Connector success rate" not in html
    assert "Live execution topology" not in html
    assert "Endpoint" not in html
    assert "Capabilities" not in html
    assert "Access token" not in html
    assert 'href="/integrations"' in html


def test_failed_jobs_are_a_jobs_status_filter():
    html = _env().get_template("logs.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/failed")),
        ui_auth_enabled=True,
        view_mode="failed",
        view_title="Failed Jobs",
        view_description="Requires attention",
        total_count=3,
        timeframe="7d",
        job_categories=[],
        category="all",
        repo_locked=False,
        repo_filter="",
        job_filter="",
        repo_options=[],
        job_options=[],
        timeframe_options=[],
        status_filter="failed",
        duration_summary=None,
        job_duration_summary=None,
        page_start=1,
        page_end=3,
        meta_label="failed jobs",
        jobs=[],
        empty_title="No Failed Jobs",
        empty_text="No failures",
        total_pages=1,
        page=1,
        base_path="/logs",
    )

    assert "Execution journal" in html
    assert 'aria-label="Execution modules"' not in html
    assert 'aria-label="Execution views"' in html
    assert 'href="/logs"' in html
    assert '<option value="failed" selected>Failed</option>' in html
    assert 'href="/failed"' not in html
    assert "Filter by scanner or repository" in html
