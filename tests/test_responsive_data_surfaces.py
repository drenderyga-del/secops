from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.target_navigation import product_target_url, repository_target_url


ROOT = Path(__file__).resolve().parents[1]


class _TextByIdParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}
        self._active_id = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "code" and attributes.get("id"):
            self._active_id = attributes["id"]
            self.values[self._active_id] = ""

    def handle_endtag(self, tag):
        if tag == "code":
            self._active_id = None

    def handle_data(self, data):
        if self._active_id:
            self.values[self._active_id] += data


def _env():
    env = Environment(
        loader=FileSystemLoader(ROOT / "app" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    env.globals.update(
        product_target_url=product_target_url,
        repository_target_url=repository_target_url,
    )
    return env


def test_responsive_table_macro_exposes_keyboard_scroll_contract():
    template = _env().from_string(
        """
        {% import 'macros/data_surfaces.html' as data_surfaces %}
        {% call data_surfaces.responsive_table('checks', 'checks-title', 'wide') %}
        <table class="data-table"><tr><td>value</td></tr></table>
        {% endcall %}
        """
    )

    html = template.render()

    assert 'id="checks"' in html
    assert 'role="region"' in html
    assert 'tabindex="0"' in html
    assert 'aria-labelledby="checks-title"' in html
    assert 'aria-describedby="checks-scroll-hint"' in html
    assert "Left and Right Arrow keys" in html
    assert "responsive-table-scroll-wide" in html


def test_evidence_macro_preserves_adversarial_full_value_for_expand_and_copy():
    value = "diagnostic://" + ("A" * 512) + "/<&>/'\""
    template = _env().from_string(
        """
        {% import 'macros/data_surfaces.html' as data_surfaces %}
        {{ data_surfaces.evidence(value, 'diagnostic-value', 'Diagnostic') }}
        """
    )

    html = template.render(value=value)
    parser = _TextByIdParser()
    parser.feed(html)

    assert parser.values["diagnostic-value"] == value
    assert 'data-copy-target="diagnostic-value"' in html
    assert 'data-copy-feedback="diagnostic-value-copy-feedback"' in html
    assert "Expand full diagnostic" in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert value not in html.split('<pre><code id="diagnostic-value">', 1)[0]


def test_target_360_keeps_each_long_diagnostic_exact_in_shared_disclosure():
    values = {
        "control": "control:" + ("C" * 400),
        "execution": "execution:" + ("E" * 400),
        "step": "step:" + ("S" * 400),
        "delivery": "delivery:" + ("D" * 400),
        "event": "event:" + ("V" * 400),
    }
    html = _env().get_template("repository_360.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/targets/repositories/7")),
        ui_auth_enabled=True,
        repository=SimpleNamespace(id=7, path="platform/example_app/api"),
        product=SimpleNamespace(name="ExampleApp", id=3),
        gitlab_repository_url=None,
        job_health={"broken_keys": 0, "failures": [], "failures_href": "/logs"},
        coverage={"expected": 1, "configured": 0, "missing": 1, "excluded": 0},
        freshness={"state": "stale", "label": "stale", "observed_at": None},
        summary={
            "executions": 1,
            "artifacts": 1,
            "confirmed_deliveries": 0,
            "failed_executions": 1,
        },
        versions=[],
        controls=[
            {
                "key": "sast",
                "name": "SAST",
                "category": "code",
                "scope": "*",
                "status": "missing",
                "source": "gitlab",
                "reason": values["control"],
                "evidence": None,
                "last_verified_at": None,
            }
        ],
        source_options=[{"value": "all", "label": "All sources", "count": 1}],
        selected_source="all",
        executions=[
            {
                "source_label": "IaC",
                "correlation_key": "iac:42:100",
                "status": "failed",
                "failure_category": "delivery",
                "failure_reason": values["execution"],
                "requested_at": None,
                "steps": [
                    {
                        "name": "Checkov scan",
                        "status": "failed",
                        "source": "checkov",
                        "failure_reason": values["step"],
                    }
                ],
                "artifacts": [
                    {
                        "kind": "sarif",
                        "schema_version": "2.1.0",
                        "digest_sha256": "b" * 64,
                        "provenance": None,
                        "deliveries": [
                            {
                                "destination": "asoc",
                                "status": "failed",
                                "attempt_key": "initial",
                                "failure_reason": values["delivery"],
                            }
                        ],
                    }
                ],
                "events": [
                    {
                        "event_type": "execution.failed",
                        "source": "iac_repository",
                        "reason": values["event"],
                        "observed_at": "2026-07-21T01:57:00+00:00",
                        "received_at": None,
                    }
                ],
            }
        ],
    )
    parser = _TextByIdParser()
    parser.feed(html)

    assert parser.values == {
        "target-control-1-evidence": values["control"],
        "target-execution-1-failure": values["execution"],
        "target-execution-1-step-1-failure": values["step"],
        "target-execution-1-artifact-1-delivery-1-failure": values["delivery"],
        "target-execution-1-event-1-reason": values["event"],
    }
    assert 'class="table-container target360-controls-table has-responsive-table"' in html
    assert 'id="target-controls-table"' in html
    assert 'aria-label="Repository security control coverage"' in html
    assert 'aria-labelledby="target-controls-title"' in html


def test_wide_data_tables_adopt_the_shared_contract():
    expected = {
        "repository_360.html": ("target-controls-table", "wide"),
        "pipelines.html": ("pipelines-data-table", "standard"),
        "ptai_scans.html": ("ptai-scans-table", "xwide"),
        "iac_repositories.html": ("iac-repositories-table", "xwide"),
    }

    for template_name, (table_id, width) in expected.items():
        source = (ROOT / "app" / "templates" / template_name).read_text(
            encoding="utf-8"
        )
        assert "macros/data_surfaces.html" in source
        assert "has-responsive-table" in source
        assert f"'{table_id}'" in source
        if width != "standard":
            assert f"'{width}'" in source


def test_outer_and_scroll_region_landmarks_have_unique_accessible_names():
    pipelines = (ROOT / "app" / "templates" / "pipelines.html").read_text(
        encoding="utf-8"
    )
    target360 = (ROOT / "app" / "templates" / "repository_360.html").read_text(
        encoding="utf-8"
    )

    assert (
        'id="pipelines" class="page-section table-container '
        'pipelines-table-container has-responsive-table" '
        'aria-label="Pipeline result set"'
    ) in pipelines
    assert (
        "data_surfaces.responsive_table("
        "'pipelines-data-table', 'pipeline-list-title'"
    ) in pipelines
    assert (
        'id="controls" class="page-section target360-section" '
        'aria-label="Repository security control coverage"'
    ) in target360
    assert (
        "data_surfaces.responsive_table("
        "'target-controls-table', 'target-controls-title', 'wide'"
    ) in target360


def test_narrow_desktop_table_styles_keep_columns_readable_and_scroll_visible():
    styles = (ROOT / "app" / "static" / "css" / "signal-river.css").read_text(
        encoding="utf-8"
    )
    javascript = (ROOT / "app" / "static" / "js" / "main.js").read_text(
        encoding="utf-8"
    )

    assert ".responsive-table-scroll-standard > .data-table" in styles
    assert "min-width: 820px;" in styles
    assert ".responsive-table-scroll-wide > .data-table" in styles
    assert "min-width: 960px;" in styles
    assert ".responsive-table-scroll-xwide > .data-table" in styles
    assert "min-width: 1420px;" in styles
    assert ".responsive-table-scroll:focus-visible" in styles
    assert ".responsive-table.has-overflow.at-start:not(.at-end)" in styles
    assert "overflow-wrap: anywhere;" in styles
    assert "-webkit-line-clamp: 2;" in styles
    assert "function setupResponsiveTables()" in javascript
    assert "scrollRegion.scrollWidth > scrollRegion.clientWidth + 1" in javascript
    assert "root.classList.toggle('has-overflow', hasOverflow)" in javascript
    assert "container.insertBefore(searchInput, scrollRegion || table)" in javascript
