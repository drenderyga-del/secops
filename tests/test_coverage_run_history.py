from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

from app.main import _audit_restart_diagnostic
from app.models import AuditScanRun
from app.routers.web import audit
from app.target_navigation import (
    COVERAGE_RUNS_PATH,
    COVERAGE_RUN_PATH,
    coverage_run_url,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("previous_status", "message", "error"),
    [
        (
            "queued",
            "Queued audit scan did not start before service restart",
            "Not started before app restart",
        ),
        (
            "running",
            "Audit scan interrupted by service restart",
            "Interrupted by app restart",
        ),
    ],
)
def test_restart_diagnostic_distinguishes_queued_from_started_runs(
    previous_status,
    message,
    error,
):
    assert _audit_restart_diagnostic(previous_status) == (message, error)


def _run(run_id=21, *, diagnostic="Scan completed"):
    started_at = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=run_id,
        status="failed" if diagnostic != "Scan completed" else "completed",
        requested_product_ids="3,8",
        selected_products=2,
        total_repositories=12,
        processed_repositories=9,
        message="Scan completed",
        error_message=diagnostic if diagnostic != "Scan completed" else None,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=2, seconds=7),
        created_at=started_at - timedelta(seconds=2),
        updated_at=started_at + timedelta(minutes=2, seconds=7),
    )


def test_run_serializer_preserves_full_diagnostic_and_exposes_short_summary():
    diagnostic = "Database timeout\n" + ("repository fetch failed; " * 30)

    serialized = audit._serialize_scan_run(_run(diagnostic=diagnostic))

    assert serialized["url"] == "/coverage/runs/21"
    assert serialized["status_label"] == "Failed"
    assert serialized["status_class"] == "error"
    assert serialized["diagnostic"] == diagnostic
    assert len(serialized["diagnostic_excerpt"]) <= 180
    assert serialized["diagnostic_excerpt"].endswith("…")
    assert serialized["diagnostic_is_truncated"] is True
    assert serialized["duration"] == "2m 7s"


def test_history_paginates_runs_and_builds_deep_links(monkeypatch):
    runs = [_run(5), _run(4)]

    class CountQuery:
        def scalar(self):
            return 25

    class RunsQuery:
        offset_value = None
        limit_value = None

        def order_by(self, *_args):
            return self

        def offset(self, value):
            self.offset_value = value
            return self

        def limit(self, value):
            self.limit_value = value
            return self

        def all(self):
            return runs

    run_query = RunsQuery()

    class FakeDb:
        def query(self, entity):
            return run_query if entity is AuditScanRun else CountQuery()

    captured = {}

    def capture_template(name, context):
        captured.update(name=name, context=context)
        return captured

    monkeypatch.setattr(audit.templates, "TemplateResponse", capture_template)
    response = asyncio.run(
        audit.coverage_run_history(
            request=SimpleNamespace(),
            page=2,
            db=FakeDb(),
        )
    )

    assert response["name"] == "coverage_runs.html"
    assert run_query.offset_value == 20
    assert run_query.limit_value == 20
    assert [run["url"] for run in response["context"]["runs"]] == [
        "/coverage/runs/5",
        "/coverage/runs/4",
    ]
    assert response["context"]["pagination"] == {
        "page": 2,
        "pages": 2,
        "total": 25,
        "previous_url": "/coverage/runs?page=1",
        "next_url": None,
    }


def test_run_detail_returns_404_for_unknown_run(monkeypatch):
    class MissingQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    missing_db = SimpleNamespace(query=lambda *_args: MissingQuery())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            audit.coverage_run_detail(
                request=SimpleNamespace(),
                run_id=404,
                db=missing_db,
            )
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Coverage run not found"


def test_overview_history_and_detail_have_distinct_readable_contracts():
    overview = (ROOT / "app/templates/audit.html").read_text(encoding="utf-8")
    history = (ROOT / "app/templates/coverage_runs.html").read_text(encoding="utf-8")
    detail = (
        ROOT / "app/templates/coverage_run_detail.html"
    ).read_text(encoding="utf-8")
    styles = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
    script = (ROOT / "app/static/js/main.js").read_text(encoding="utf-8")

    assert "Audit tasks" not in overview
    assert "scan_tasks" not in inspect.getsource(audit.coverage_view)
    assert "<h2>Coverage</h2>" in overview
    assert "local_nav.toc(" not in overview
    assert "Run history" in overview
    assert 'href="{{ scan_run.url }}">Open run</a>' in overview

    assert "<table" not in history
    assert "coverage-run-row" in history
    assert "Open run" in history
    assert "Page {{ pagination.page }} of {{ pagination.pages }}" in history
    assert "Current repository state remains on the Coverage overview." in history

    assert "<details" in detail
    assert "Expand full diagnostic" in detail
    assert 'data-copy-target="coverage-run-diagnostic"' in detail
    assert 'aria-live="polite"' in detail
    assert "setupCopyButtons();" in script
    assert "navigator.clipboard.writeText" in script
    assert "cleanUrl.searchParams.delete('run_id');" in script
    assert 'AuditScanRun.status.in_(["running", "queued"])' in inspect.getsource(
        audit.coverage_view
    )

    assert "@media (max-width: 900px)" in styles
    assert ".coverage-run-row {" in styles
    assert "grid-template-columns: minmax(0, 1fr) auto;" in styles
    assert ".audit-metrics-grid { grid-template-columns: repeat(2" in styles

    environment = Environment(
        loader=FileSystemLoader(ROOT / "app/templates"),
        autoescape=True,
    )
    for template in ("audit.html", "coverage_runs.html", "coverage_run_detail.html"):
        environment.get_template(template)

    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    assert "/static/css/style.css?v=20260730-local-toc-removal-1" in base
    assert "/static/css/signal-control.css?v=20260923-asoc-product-lifecycle-1" in base
    assert "/static/js/main.js?v=20260805-filter-surface-2" in base


def test_coverage_run_paths_are_explicit_and_stable():
    assert COVERAGE_RUNS_PATH == "/coverage/runs"
    assert COVERAGE_RUN_PATH == "/coverage/runs/{run_id}"
    assert coverage_run_url(17) == "/coverage/runs/17"
