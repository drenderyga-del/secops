from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.pipeline_query_service import normalize_pipeline_filters
from app.pipeline_view_contract import PipelineLifecycleGroup
from app.routers.web import pipelines as pipelines_router
from app.routers.web.common import get_db


ROOT = Path(__file__).resolve().parents[1]


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def _result(filters, *, total=12, matching=4, counts=None):
    return SimpleNamespace(
        rows=[],
        filters=filters,
        total_count=total,
        matching_count=matching,
        group_counts=counts
        or {
            PipelineLifecycleGroup.NEEDS_ATTENTION: 3,
            PipelineLifecycleGroup.IN_PROGRESS: 4,
            PipelineLifecycleGroup.COMPLETED_RECENTLY: 5,
        },
        page_count=1,
    )


def _client(monkeypatch, result_factory):
    class StubService:
        def __init__(self, _db):
            pass

        def fetch(self, filters):
            return result_factory(filters)

    monkeypatch.setattr(pipelines_router, "PipelineQueryService", StubService)
    app = FastAPI()
    app.include_router(pipelines_router.router)
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app)


def _summary_links(html):
    parser = _LinkParser()
    parser.feed(html)
    links = {}
    for attrs in parser.links:
        for class_name in attrs.get("class", "").split():
            if class_name.startswith("pipeline-group-") and class_name != "pipeline-group-link":
                links[class_name.removeprefix("pipeline-group-")] = attrs
    return links


def test_default_summary_groups_show_counts_copy_and_keyboard_selection(monkeypatch):
    with _client(monkeypatch, _result) as client:
        response = client.get("/pipelines/operator")

    assert response.status_code == 200
    assert "<title>Pipelines · ScanOps</title>" in response.text
    assert "<h1>Pipelines</h1>" in response.text
    assert "Active Pipelines</h1>" not in response.text
    assert "Total retained" in response.text
    for label, copy in (
        ("Needs attention", "Failed and stale pipelines in the selected period."),
        ("In progress", "Registered and polling pipelines in the selected period."),
        ("Completed recently", "Completed pipelines in the selected period."),
    ):
        assert label in response.text
        assert copy in response.text

    links = _summary_links(response.text)
    assert set(links) == {"needs_attention", "in_progress", "completed_recently"}
    assert links["in_progress"]["aria-current"] == "page"
    assert "is-selected" in links["in_progress"]["class"]
    assert "aria-current" not in links["needs_attention"]


def test_summary_links_preserve_scope_period_and_page_size_but_reset_status_and_page(
    monkeypatch,
):
    with _client(monkeypatch, _result) as client:
        response = client.get(
            "/pipelines/operator?group=needs_attention&status=stale&product=4"
            "&repository=9&timeframe=30d&page=7&page_size=50"
        )

    assert response.status_code == 200
    links = _summary_links(response.text)
    selected = links["needs_attention"]
    assert selected["aria-current"] == "page"
    for value, attrs in links.items():
        query = parse_qs(urlsplit(attrs["href"]).query)
        assert query == {
            "group": [value],
            "product": ["4"],
            "repository": ["9"],
            "timeframe": ["30d"],
            "page": ["1"],
            "page_size": ["50"],
        }

    assert '<option value="30d" selected>Last 30 days</option>' in response.text
    assert '<option value="stale" selected>Stale</option>' in response.text
    assert "Product #4" in response.text
    assert "Repository #9" in response.text


def test_empty_state_copy_distinguishes_baseline_groups_and_explicit_filters():
    empty_state = pipelines_router._pipeline_empty_state
    counts = {
        "needs_attention": 0,
        "in_progress": 0,
        "completed_recently": 0,
    }

    baseline = empty_state(
        normalize_pipeline_filters(),
        total_count=0,
        matching_count=0,
        group_counts=counts,
    )
    assert baseline["heading"] == "No pipelines recorded yet"
    assert "/api/pipelines/register" in baseline["body"]

    expected = {
        "in_progress": "No pipelines are in progress",
        "needs_attention": "No pipelines need attention",
        "completed_recently": "No pipelines completed in this period",
    }
    for group, heading in expected.items():
        state = empty_state(
            normalize_pipeline_filters(group=group),
            total_count=12,
            matching_count=0,
            group_counts=counts,
        )
        assert state["heading"] == heading
        assert state["actions"][0]["label"] == "Show all retained history"

    filtered = empty_state(
        normalize_pipeline_filters(group="needs_attention", status="failed"),
        total_count=12,
        matching_count=0,
        group_counts={**counts, "needs_attention": 2},
    )
    assert filtered["heading"] == "No pipelines match these filters"
    assert filtered["actions"][0]["label"] == "Clear filters"


def test_database_failure_renders_actionable_503_without_internal_error_text(monkeypatch):
    error = OperationalError("SELECT active_pipelines", {}, RuntimeError("offline"))

    def fail(_filters):
        raise error

    with _client(monkeypatch, fail) as client:
        response = client.get(
            "/pipelines/operator?group=needs_attention&timeframe=30d",
            follow_redirects=False,
        )

    assert response.status_code == 503
    assert "Pipelines could not be loaded" in response.text
    assert "Retry this view" in response.text
    assert "Return to default" in response.text
    assert "SELECT active_pipelines" not in response.text


def test_summary_and_filter_layout_has_scoped_responsive_contract():
    template = (ROOT / "app" / "templates" / "pipelines.html").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "app" / "static" / "css" / "style.css").read_text(
        encoding="utf-8"
    )

    assert 'aria-label="Pipeline lifecycle group"' in template
    assert 'aria-current="page"' in template
    assert "{% set pipelines_url = '/pipelines' %}" in template
    assert 'method="get" action="{{ pipelines_url }}"' in template
    assert ".pipeline-group-switcher {" in styles
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in styles
    assert "@media (max-width: 900px)" in styles
    assert "@media (max-width: 620px)" in styles
