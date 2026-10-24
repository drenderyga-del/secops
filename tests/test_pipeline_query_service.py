from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import ActivePipeline, PipelineJob, Product, Repository
from app.models.pipeline import PipelineStatus
from app.pipeline_query_service import (
    PipelineQueryService,
    normalize_pipeline_filters,
)
from app.pipeline_view_contract import PipelineLifecycleGroup
from app.routers.web import pipelines as pipelines_router
from app.routers.web import signal_control as signal_control_router
from app.routers.web.common import get_db


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (
        Product.__table__,
        Repository.__table__,
        ActivePipeline.__table__,
        PipelineJob.__table__,
    ):
        table.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seed_scope(db):
    product = Product(
        name="Platform",
        path="platform",
        gitlab_base_url="https://gitlab.example.com",
    )
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/api")
    other_repository = Repository(product_id=product.id, path="platform/web")
    db.add_all([repository, other_repository])
    db.flush()
    return product, repository, other_repository


def _pipeline(
    db,
    *,
    product,
    repository,
    number,
    status=PipelineStatus.POLLING,
    activity_at,
):
    row = ActivePipeline(
        project_id=1000 + number,
        pipeline_id=2000 + number,
        product_id=product.id,
        repository_id=repository.id,
        status=status,
        created_at=activity_at,
        updated_at=activity_at,
        last_polled_at=activity_at,
        completed_at=activity_at if status in (PipelineStatus.COMPLETED, PipelineStatus.FAILED) else None,
        stale_at=activity_at if status is PipelineStatus.STALE else None,
    )
    db.add(row)
    return row


def test_filter_normalization_derives_group_and_never_returns_422_values():
    filters = normalize_pipeline_filters(
        status="failed",
        product="-4",
        repository="not-an-id",
        timeframe="unsupported",
        page="zero",
        page_size="500",
    )

    assert filters.group is PipelineLifecycleGroup.NEEDS_ATTENTION
    assert filters.status is PipelineStatus.FAILED
    assert filters.product_id is None
    assert filters.repository_id is None
    assert filters.timeframe == "7d"
    assert filters.page == 1
    assert filters.page_size == 25

    assert normalize_pipeline_filters(page_size="100").page_size == 100

    incompatible = normalize_pipeline_filters(group="in_progress", status="failed")
    assert incompatible.group is PipelineLifecycleGroup.IN_PROGRESS
    assert incompatible.status is PipelineStatus.FAILED


def test_execution_health_counts_match_status_drilldown_rows(db):
    product, repository, _ = _seed_scope(db)
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    for number, status, age in (
        (1, PipelineStatus.COMPLETED, timedelta(hours=1)),
        (2, PipelineStatus.COMPLETED, timedelta(days=2)),
        (3, PipelineStatus.FAILED, timedelta(days=3)),
        (4, PipelineStatus.POLLING, timedelta(hours=2)),
        (5, PipelineStatus.STALE, timedelta(hours=3)),
        (6, PipelineStatus.COMPLETED, timedelta(days=8)),
    ):
        _pipeline(
            db,
            product=product,
            repository=repository,
            number=number,
            status=status,
            activity_at=now - age,
        )
    db.commit()

    service = PipelineQueryService(db)
    health = service.execution_health("7d", request_now=now)

    assert health.completed == 2
    assert health.failed == 1
    assert health.total == 3
    assert health.success_rate == 66.7
    assert health.failure_rate == 33.3

    for status, expected in (
        (PipelineStatus.COMPLETED, health.completed),
        (PipelineStatus.FAILED, health.failed),
    ):
        result = service.fetch(
            normalize_pipeline_filters(status=status.value, timeframe="7d"),
            request_now=now,
        )
        assert result.matching_count == expected
        assert len(result.rows) == expected
        assert {row.pipeline.status for row in result.rows} == {status}

    year_health = service.execution_health("1y", request_now=now)
    year_completed = service.fetch(
        normalize_pipeline_filters(status="completed", timeframe="1y"),
        request_now=now,
    )
    assert year_health.completed == year_completed.matching_count == 3


def test_251_equal_timestamp_rows_are_reachable_with_stable_id_tie_break(db):
    product, repository, _ = _seed_scope(db)
    now = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    for number in range(251):
        _pipeline(
            db,
            product=product,
            repository=repository,
            number=number,
            activity_at=now - timedelta(minutes=1),
        )
    db.commit()

    seen = []
    for page in range(1, 7):
        filters = normalize_pipeline_filters(timeframe="all", page=page, page_size=50)
        result = PipelineQueryService(db).fetch(filters, request_now=now)
        seen.extend(row.pipeline.id for row in result.rows)
        assert result.total_count == 251
        assert result.matching_count == 251
        assert result.page_count == 6

    assert len(seen) == 251
    assert len(set(seen)) == 251
    assert seen == sorted(seen, reverse=True)

    beyond_last = PipelineQueryService(db).fetch(
        normalize_pipeline_filters(timeframe="all", page=999, page_size=50),
        request_now=now,
    )
    assert beyond_last.filters.page == 6
    assert [row.pipeline.id for row in beyond_last.rows] == seen[-1:]


def test_server_filters_counts_and_status_specific_activity_timestamp(db):
    product, repository, other_repository = _seed_scope(db)
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    polling = _pipeline(
        db,
        product=product,
        repository=repository,
        number=1,
        status=PipelineStatus.POLLING,
        activity_at=now - timedelta(hours=1),
    )
    polling.created_at = now - timedelta(days=90)
    _pipeline(
        db,
        product=product,
        repository=repository,
        number=2,
        status=PipelineStatus.STALE,
        activity_at=now - timedelta(days=2),
    )
    _pipeline(
        db,
        product=product,
        repository=other_repository,
        number=3,
        status=PipelineStatus.COMPLETED,
        activity_at=now - timedelta(days=10),
    )
    db.commit()

    active = PipelineQueryService(db).fetch(
        normalize_pipeline_filters(
            group="in_progress",
            product=product.id,
            repository=repository.id,
            timeframe="7d",
        ),
        request_now=now,
    )
    assert [row.pipeline.id for row in active.rows] == [polling.id]
    assert active.total_count == 3
    assert active.matching_count == 1
    assert active.group_counts == {
        PipelineLifecycleGroup.NEEDS_ATTENTION: 1,
        PipelineLifecycleGroup.IN_PROGRESS: 1,
        PipelineLifecycleGroup.COMPLETED_RECENTLY: 0,
    }

    completed = PipelineQueryService(db).fetch(
        normalize_pipeline_filters(
            group="completed_recently",
            repository=other_repository.id,
            timeframe="7d",
        ),
        request_now=now,
    )
    assert completed.matching_count == 0
    assert completed.rows == []


def test_inconsistent_product_repository_scope_fails_closed_for_rows_counts_and_facets(db):
    product_a, repository_a, _ = _seed_scope(db)
    product_b = Product(
        name="ExampleApp",
        path="example_app",
        gitlab_base_url="https://gitlab.example.com",
    )
    db.add(product_b)
    db.flush()
    repository_b = Repository(product_id=product_b.id, path="example_app/api")
    db.add(repository_b)
    db.flush()

    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    _pipeline(
        db,
        product=product_a,
        repository=repository_a,
        number=1,
        status=PipelineStatus.POLLING,
        activity_at=now - timedelta(hours=1),
    )
    _pipeline(
        db,
        product=product_b,
        repository=repository_b,
        number=2,
        status=PipelineStatus.FAILED,
        activity_at=now - timedelta(hours=2),
    )
    db.commit()

    result = PipelineQueryService(db).fetch(
        normalize_pipeline_filters(
            group="in_progress",
            product=product_a.id,
            repository=repository_b.id,
            timeframe="all",
        ),
        request_now=now,
    )

    assert result.filters.product_id == product_a.id
    assert result.filters.repository_id == repository_b.id
    assert result.total_count == 2
    assert result.matching_count == 0
    assert result.rows == []
    assert result.group_counts == {
        PipelineLifecycleGroup.NEEDS_ATTENTION: 0,
        PipelineLifecycleGroup.IN_PROGRESS: 0,
        PipelineLifecycleGroup.COMPLETED_RECENTLY: 0,
    }
    assert result.page_count == 1


def test_job_counts_use_one_paginated_query_and_do_not_scale_per_row(db):
    product, repository, _ = _seed_scope(db)
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    for number in range(50):
        pipeline = _pipeline(
            db,
            product=product,
            repository=repository,
            number=number,
            activity_at=now - timedelta(minutes=1),
        )
        db.flush()
        db.add_all(
            [
                PipelineJob(
                    active_pipeline_id=pipeline.id,
                    gitlab_job_id=number * 10 + offset,
                    name=f"job-{offset}",
                    status="success",
                )
                for offset in range(number % 3)
            ]
        )
    db.commit()

    select_count = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(db.bind, "before_cursor_execute", count_selects)
    try:
        result = PipelineQueryService(db).fetch(
            normalize_pipeline_filters(timeframe="all", page_size=50),
            request_now=now,
        )
    finally:
        event.remove(db.bind, "before_cursor_execute", count_selects)

    assert select_count == 4
    assert len(result.rows) == 50
    assert {row.pipeline.pipeline_id: row.job_count for row in result.rows} == {
        2000 + number: number % 3 for number in range(50)
    }


def test_route_exposes_counts_pagination_and_normalized_server_filters(db):
    product, repository, _ = _seed_scope(db)
    now = datetime.now(timezone.utc)
    for number in range(26):
        _pipeline(
            db,
            product=product,
            repository=repository,
            number=number,
            activity_at=now - timedelta(minutes=1),
        )
    db.commit()

    app = FastAPI()
    app.include_router(pipelines_router.router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    response = TestClient(app).get(
        "/pipelines/operator?timeframe=all&page_size=25&page=1&product="
        f"{product.id}&repository={repository.id}"
    )

    assert response.status_code == 200
    assert "Showing <strong>26</strong> of <strong>26</strong> pipelines" in response.text
    assert "Page 1 of 2" in response.text
    assert "page=2" in response.text
    assert "page_size=25" in response.text


def test_signal_control_pipeline_api_exposes_scope_and_never_invents_progress(db):
    product, repository, repository_without_pipeline = _seed_scope(db)
    now = datetime.now(timezone.utc)
    for number in range(51):
        _pipeline(
            db,
            product=product,
            repository=repository,
            number=number,
            status=PipelineStatus.POLLING,
            activity_at=now - timedelta(minutes=1),
        )
    failed = _pipeline(
        db,
        product=product,
        repository=repository,
        number=100,
        status=PipelineStatus.FAILED,
        activity_at=now - timedelta(minutes=2),
    )
    db.commit()

    app = FastAPI()
    app.include_router(signal_control_router.router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    page = client.get(
        "/api/ui/pipelines"
        f"?timeframe=all&product={product.id}&repository={repository.id}"
        "&page=1&page_size=50"
    )
    assert page.status_code == 200
    payload = page.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 50
    assert payload["page_count"] == 2
    assert len(payload["rows"]) == 50
    assert payload["filter_options"]["products"] == [
        {"id": product.id, "name": product.name, "path": product.path}
    ]
    assert payload["filter_options"]["repositories"] == [
        {
            "id": repository.id,
            "path": repository.path,
            "product_id": product.id,
        }
    ]
    assert repository_without_pipeline.id not in {
        option["id"]
        for option in payload["filter_options"]["repositories"]
    }
    assert all("progress" not in row for row in payload["rows"])

    failure = client.get(
        "/api/ui/pipelines"
        "?group=needs_attention&status=failed&timeframe=all&page_size=25"
    )
    assert failure.status_code == 200
    failure_rows = failure.json()["rows"]
    assert [row["id"] for row in failure_rows] == [failed.id]
    assert failure_rows[0]["status"] == "failed"
    assert "progress" not in failure_rows[0]

    status_only = client.get(
        "/api/ui/pipelines?status=failed&timeframe=all&page_size=25"
    )
    assert status_only.status_code == 200
    status_only_payload = status_only.json()
    assert status_only_payload["group"] == "needs_attention"
    assert status_only_payload["status"] == "failed"
    assert status_only_payload["matching"] == 1
    assert [row["id"] for row in status_only_payload["rows"]] == [failed.id]

    incompatible = client.get(
        "/api/ui/pipelines"
        "?group=in_progress&status=failed&timeframe=all&page_size=25"
    )
    assert incompatible.status_code == 200
    incompatible_payload = incompatible.json()
    assert incompatible_payload["group"] == "in_progress"
    assert incompatible_payload["status"] == "failed"
    assert incompatible_payload["matching"] == 0
    assert incompatible_payload["rows"] == []


def test_signal_control_pipeline_detail_exposes_persisted_stages_and_truthful_scope(db):
    product, repository, _ = _seed_scope(db)
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    empty = _pipeline(
        db,
        product=product,
        repository=repository,
        number=1,
        status=PipelineStatus.POLLING,
        activity_at=now,
    )
    single = _pipeline(
        db,
        product=product,
        repository=repository,
        number=2,
        status=PipelineStatus.COMPLETED,
        activity_at=now,
    )
    single.tracked_jobs = ["compile", "security"]
    db.flush()
    db.add(
        PipelineJob(
            active_pipeline_id=single.id,
            gitlab_job_id=2201,
            name="compile",
            stage="build",
            status="success",
            web_url="https://gitlab.example.com/platform/api/-/jobs/2201",
        )
    )
    db.add_all(
        [
            PipelineJob(
                active_pipeline_id=single.id,
                gitlab_job_id=2200,
                name="compile",
                stage="build",
                status="failed",
            ),
            PipelineJob(
                active_pipeline_id=single.id,
                gitlab_job_id=2202,
                name="obsolete-job",
                stage="legacy",
                status="failed",
            ),
        ]
    )
    multiple = _pipeline(
        db,
        product=product,
        repository=repository,
        number=3,
        status=PipelineStatus.FAILED,
        activity_at=now,
    )
    db.flush()
    db.add_all(
        [
            PipelineJob(
                active_pipeline_id=multiple.id,
                gitlab_job_id=2301,
                name="compile",
                stage="security",
                status="success",
            ),
            PipelineJob(
                active_pipeline_id=multiple.id,
                gitlab_job_id=2299,
                name="compile",
                stage="security",
                status="failed",
            ),
            PipelineJob(
                active_pipeline_id=multiple.id,
                gitlab_job_id=2302,
                name="security",
                stage="build",
                status="failed",
            ),
            PipelineJob(
                active_pipeline_id=multiple.id,
                gitlab_job_id=2303,
                name="cleanup",
                stage="cleanup",
                status="canceled",
            ),
            PipelineJob(
                active_pipeline_id=multiple.id,
                gitlab_job_id=2304,
                name="optional-check",
                stage="security",
                status="skipped",
            ),
        ]
    )
    db.commit()

    app = FastAPI()
    app.include_router(signal_control_router.router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    empty_payload = client.get(f"/api/ui/pipelines/{empty.id}").json()
    assert empty_payload["pipeline"]["id"] == empty.id
    assert empty_payload["stages"] == []
    assert empty_payload["completeness"]["jobs_complete"] is None
    assert empty_payload["completeness"]["known_jobs"] == 0

    single_payload = client.get(f"/api/ui/pipelines/{single.id}").json()
    assert single_payload["completeness"] == {
        "mode": "tracked_only",
        "is_partial": True,
        "jobs_complete": False,
        "known_jobs": 1,
        "expected_jobs": None,
        "tracked_job_names": ["compile", "security"],
        "message": "Only jobs named by the registration contract are persisted; other GitLab jobs may be absent.",
    }
    assert single_payload["stages"][0]["status"] == "completed"
    assert single_payload["completeness"]["known_jobs"] == 1
    assert single_payload["stages"][0]["jobs"][0]["gitlab_job_id"] == 2201
    assert single_payload["stages"][0]["jobs"][0]["links"]["gitlab"].endswith("/2201")
    assert single_payload["stages"][0]["jobs"][0]["allow_failure"] is None

    multiple_payload = client.get(f"/api/ui/pipelines/{multiple.id}").json()
    assert [stage["name"] for stage in multiple_payload["stages"]] == [
        "build",
        "cleanup",
        "security",
    ]
    assert [stage["status"] for stage in multiple_payload["stages"]] == [
        "failed",
        "warning",
        "completed",
    ]
    assert multiple_payload["ordering"]["mode"] == "alphabetical_name"
    assert "declared CI stage order" in multiple_payload["ordering"]["message"]
    assert multiple_payload["stages"][2]["succeeded"] == 1
    assert multiple_payload["stages"][2]["total"] == 2
    assert multiple_payload["summary"]["jobs"] == 4


def test_signal_control_pipeline_detail_uses_internal_id_and_returns_404(db):
    app = FastAPI()
    app.include_router(signal_control_router.router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    response = TestClient(app).get("/api/ui/pipelines/999999")

    assert response.status_code == 404
