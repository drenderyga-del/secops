from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import DatabaseService
from app.models import JobReport, Product, Repository
from app.models.job_report import JobStatus
from app.models.repository import RepositoryStatus
from app.routers.web import logs as logs_router
from app.routers.web.signal_control import (
    signal_control_failed_jobs,
    signal_control_job_journal,
)
from app.url_safety import safe_external_http_url


SAFE_PIPELINE_URL = "https://gitlab.example.com/platform/service/-/pipelines/42"
UNSAFE_PIPELINE_URL = "javascript:alert(document.domain)"


@pytest.fixture
def job_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (Product.__table__, Repository.__table__, JobReport.__table__):
        table.create(engine)

    db = sessionmaker(bind=engine)()
    product = Product(
        name="Platform",
        path="platform",
        gitlab_base_url="https://gitlab.example.com",
    )
    db.add(product)
    db.flush()
    repository = Repository(
        product_id=product.id,
        path="platform/service",
        overall_status=RepositoryStatus.FAILED,
    )
    db.add(repository)
    db.flush()
    finished_at = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            JobReport(
                repository_id=repository.id,
                job_name="unsafe-legacy-job",
                job_status=JobStatus.FAILED,
                pipeline_url=UNSAFE_PIPELINE_URL,
                finished_at=finished_at,
            ),
            JobReport(
                repository_id=repository.id,
                job_name="safe-job",
                job_status=JobStatus.FAILED,
                pipeline_url=SAFE_PIPELINE_URL,
                finished_at=finished_at,
            ),
        ]
    )
    db.commit()

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    "value",
    [
        UNSAFE_PIPELINE_URL,
        "data:text/html,<script>alert(1)</script>",
        "//gitlab.example.com/platform/-/pipelines/42",
        "https:///platform/-/pipelines/42",
        "https://user:secret@gitlab.example.com/platform/-/pipelines/42",
        "https://gitlab.example.com\\@attacker.example/pipeline",
        "https://gitlab.example.com:99999/pipeline",
        None,
        42,
    ],
)
def test_external_http_url_helper_fails_closed(value):
    assert safe_external_http_url(value) is None


def test_signal_control_outputs_do_not_expose_unsafe_legacy_links(job_db):
    failed_jobs = signal_control_failed_jobs(limit=100, db=job_db)
    journal = signal_control_job_journal(
        repo="",
        job="",
        category="all",
        status="all",
        timeframe="all",
        page=1,
        page_size=20,
        db=job_db,
    )

    for payload in (failed_jobs, journal):
        rows = {row["name"]: row for row in payload["rows"]}
        assert rows["unsafe-legacy-job"]["pipeline_url"] is None
        assert rows["safe-job"]["pipeline_url"] == SAFE_PIPELINE_URL


def test_logs_template_only_links_safe_persisted_pipeline_urls(job_db, monkeypatch):
    class CapturingTemplates:
        def TemplateResponse(self, template_name, context):
            assert template_name == "logs.html"
            return context

    monkeypatch.setattr(logs_router, "templates", CapturingTemplates())
    request = SimpleNamespace(
        url=SimpleNamespace(path="/logs"),
        query_params={},
    )
    context = logs_router._render_jobs_page(
        request,
        DatabaseService(job_db),
        repo="",
        job="",
        category="all",
        status="all",
        page=1,
        only_failed=False,
        timeframe="all",
    )

    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    env.filters["duration"] = lambda value: str(value or "")
    env.filters["urlencode"] = lambda value: str(value or "")
    html = env.get_template("logs.html").render(
        **context,
        ui_auth_enabled=True,
    )

    assert context["pipeline_urls_by_job_id"] == {
        row.id: (SAFE_PIPELINE_URL if row.job_name == "safe-job" else None)
        for row in context["jobs"]
    }
    assert UNSAFE_PIPELINE_URL not in html
    assert f'href="{SAFE_PIPELINE_URL}"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert html.count("View pipeline") == 1
