from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


def _render(path: str) -> str:
    env = Environment(loader=FileSystemLoader("app/templates"))
    return env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path=path), query_params={}),
        ui_auth_enabled=True,
    )


def test_operator_navigation_matches_the_signal_control_groups():
    html = _render("/")

    for label in (">Operate<", ">Understand<", ">Platform<"):
        assert label in html
    assert "side-link-icon" not in html
    assert "scope & controls" not in html


def test_repository_detail_breadcrumb_matches_the_repository_destination():
    html = _render("/targets/repositories/7")

    assert "<span>Security posture</span>" in html
    assert "<strong>Repositories</strong>" in html
    assert 'href="/targets?level=repository" class="side-link active"' in html


def test_execution_detail_breadcrumb_matches_operations_destination():
    html = _render("/repos/7/jobs")

    assert "<span>Operations</span>" in html
    assert "<strong>Executions</strong>" in html
    assert 'href="/logs" class="side-sublink active">Job journal</a>' in html


def test_scanner_fleet_breadcrumb_matches_infrastructure_destination():
    html = _render("/scanners/operator")

    assert "<span>Infrastructure</span>" in html
    assert "<strong>Scanner fleet</strong>" in html
    assert 'href="/scanners" class="side-link active"' in html


def test_coverage_uses_its_canonical_navigation_destination():
    html = _render("/coverage/products/3")

    assert "<span>Security posture</span>" in html
    assert "<strong>Coverage</strong>" in html
    assert 'href="/coverage" class="side-link active"' in html
    assert 'href="/targets?level=product" class="side-link active"' not in html
    assert 'href="/targets?level=repository" class="side-link active"' not in html


def test_explicit_entity_breadcrumb_renders_linked_ancestry():
    env = Environment(loader=FileSystemLoader("app/templates"))
    html = env.get_template("base.html").render(
        request=SimpleNamespace(
            url=SimpleNamespace(path="/targets/repositories/7")
        ),
        ui_auth_enabled=True,
        breadcrumbs=[
            {"label": "Security posture"},
            {"label": "Targets", "href": "/targets?level=product"},
            {"label": "ExampleApp", "href": "/targets/products/3"},
            {"label": "platform/example_app/api", "current": True},
        ],
    )

    assert '<a href="/targets?level=product">Targets</a>' in html
    assert '<a href="/targets/products/3">ExampleApp</a>' in html
    assert "<strong>platform/example_app/api</strong>" in html
