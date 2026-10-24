import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from app.gitlab_url import gitlab_web_origin, looks_like_gitlab_web_url, normalize_gitlab_origin
from app.database import DatabaseService
from app.audit_service import AuditService
from app.models import (
    ActivePipeline,
    PipelineStatus,
    Product,
    Repository,
    SessionLocal,
    WorkerHeartbeat,
)
from app.routers.web import _normalize_product_group_path

from app.main import app
from app.models import JobStatus
from app.routers.web import dashboard as dashboard_router

client = TestClient(app)

def test_normalize_gitlab_origin_strips_api_v4_suffix():
    assert normalize_gitlab_origin("https://gitlab.example.com/api/v4") == "https://gitlab.example.com"
    assert normalize_gitlab_origin("https://gitlab.example.com/api/v4/") == "https://gitlab.example.com"
    assert normalize_gitlab_origin("https://gitlab.example.com") == "https://gitlab.example.com"


def test_looks_like_gitlab_web_url():
    assert looks_like_gitlab_web_url("https://gitlab.example.com/group/sub") is True
    assert looks_like_gitlab_web_url("http://gitlab.example.com") is True
    assert looks_like_gitlab_web_url("platform/infrastructure") is False
    assert looks_like_gitlab_web_url("gitlab.example.com/group") is False
    assert looks_like_gitlab_web_url("") is False


def test_gitlab_web_origin_strips_path_and_api_suffix():
    assert gitlab_web_origin("https://gitlab.example.com/api/v4") == "https://gitlab.example.com"
    assert (
        gitlab_web_origin("https://gitlab.example.com/platform/infrastructure/repo")
        == "https://gitlab.example.com"
    )
    assert gitlab_web_origin("") == ""


def test_product_identity_strips_full_gitlab_url():
    assert DatabaseService._derive_product_identity("https://gitlab.example.com/sandbox/example_app/app") == (
        "example_app",
        "sandbox/example_app",
    )
    assert DatabaseService._derive_product_identity("https://gitlab.example.com/platform/app") == (
        "platform",
        "platform",
    )


def test_product_identity_keeps_sandbox_second_segment_only():
    assert DatabaseService._derive_product_identity("sandbox/example_app/mobile/app") == (
        "example_app",
        "sandbox/example_app",
    )


def test_product_identity_uses_first_segment_for_non_sandbox_products():
    assert DatabaseService._derive_product_identity("platform/test_projects/asc_test_project") == (
        "platform",
        "platform",
    )
    assert DatabaseService._derive_product_identity("example_tools/backend/api") == (
        "example_tools",
        "example_tools",
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("sandbox/example_app/mobile/app", "sandbox/example_app"),
        ("https://gitlab.example.com/sandbox/example_app/mobile/app", "sandbox/example_app"),
        ("platform/test_projects/asc_test_project", "platform"),
        ("https://gitlab.example.com/platform/test_projects/asc_test_project", "platform"),
    ],
)
def test_product_scope_normalization_matches_product_identity(path, expected):
    assert _normalize_product_group_path(path, "fallback") == expected
    assert AuditService._normalize_scope_from_product_path(path, "fallback") == expected


@pytest.mark.needs_db
def test_health_check():
    """Test health check endpoint"""
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["service"] == "security-dashboard"
    assert isinstance(payload["uptime_seconds"], int)
    assert payload["uptime_seconds"] >= 0
    assert "last_pipeline_poll_at" in payload
    assert "operational_status" in payload
    assert "degraded_reasons" in payload
    assert "ui_auth_configured" in payload
    assert "credential_store_configured" in payload


@pytest.mark.needs_db
def test_health_check_ignores_stale_unexpected_workers(monkeypatch):
    monkeypatch.setenv("HEALTH_EXPECTED_WORKERS", "pipeline_worker")
    monkeypatch.setenv("HEALTH_WORKER_STALE_SECONDS", "900")
    monkeypatch.setenv("UI_AUTH_USERNAME", "test-user")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "test-password")
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "test-key")
    worker_names = ("pipeline_worker", "iac_worker")
    db = SessionLocal()
    previous = {
        name: db.query(WorkerHeartbeat).filter_by(name=name).one_or_none()
        for name in worker_names
    }
    previous_values = {
        name: row.last_beat_at if row is not None else None
        for name, row in previous.items()
    }
    try:
        now = datetime.now(timezone.utc)
        db.merge(WorkerHeartbeat(name="pipeline_worker", last_beat_at=now))
        db.merge(
            WorkerHeartbeat(
                name="iac_worker",
                last_beat_at=now - timedelta(hours=1),
            )
        )
        db.commit()

        response = client.get("/api/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["workers"]["iac_worker"]["stale"] is True
        assert not any(
            reason == "worker heartbeat stale: iac_worker"
            for reason in payload["degraded_reasons"]
        )
    finally:
        db.query(WorkerHeartbeat).filter(WorkerHeartbeat.name.in_(worker_names)).delete(
            synchronize_session=False
        )
        for name, beat_at in previous_values.items():
            if beat_at is not None:
                db.add(WorkerHeartbeat(name=name, last_beat_at=beat_at))
        db.commit()
        db.close()


@pytest.mark.needs_db
def test_health_check_skips_pipeline_freshness_when_worker_is_disabled(monkeypatch):
    monkeypatch.setenv("HEALTH_EXPECTED_WORKERS", "none")
    monkeypatch.setenv("HEALTH_PIPELINE_POLL_STALE_SECONDS", "1")
    monkeypatch.setenv("UI_AUTH_USERNAME", "test-user")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "test-password")
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "test-key")
    db = SessionLocal()
    product = Product(
        name="health-check-disabled-worker-test",
        path="health-check-disabled-worker-test",
        gitlab_base_url="https://gitlab.example.test",
    )
    db.add(product)
    db.flush()
    repository = Repository(
        product_id=product.id,
        path="health-check-disabled-worker-test/repo",
    )
    db.add(repository)
    db.flush()
    pipeline = ActivePipeline(
        project_id=2_000_000_000,
        pipeline_id=int(datetime.now(timezone.utc).timestamp()) % 1_000_000_000,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.POLLING,
        last_polled_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(pipeline)
    db.commit()
    try:
        response = client.get("/api/health")

        assert response.status_code == 200
        payload = response.json()
        assert not any(
            reason.startswith("pipeline polling is stale")
            for reason in payload["degraded_reasons"]
        )
        assert not any(
            reason.startswith("worker heartbeat")
            for reason in payload["degraded_reasons"]
        )
    finally:
        db.delete(pipeline)
        db.delete(repository)
        db.delete(product)
        db.commit()
        db.close()

@pytest.mark.needs_db
def test_root_redirect():
    """The product root serves the Signal Control workspace."""
    response = client.get("/")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text
    assert "ScanOps — Signal Control" in response.text


@pytest.mark.needs_db
def test_classic_dashboard_redirects_to_the_canonical_overview():
    response = client.get("/classic?timeframe=30d", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/?timeframe=30d"


@pytest.mark.needs_db
def test_overview_operator_alias_redirects_to_the_canonical_overview():
    response = client.get(
        "/overview/operator?timeframe=today",
        follow_redirects=False,
    )
    assert response.status_code == 308
    assert response.headers["location"] == "/?timeframe=today"


@pytest.mark.needs_db
def test_scanner_operator_workspace_remains_available():
    response = client.get("/scanners/operator")
    assert response.status_code == 200
    assert "Security execution runtime" in response.text
    assert "Scanner adapters" in response.text
    assert "Workload window" in response.text


@pytest.mark.parametrize(
    ("path", "destination"),
    [
        ("/classic/pipelines?group=needs_attention", "/pipelines?group=needs_attention"),
        (
            "/classic/targets?level=repository",
            "/targets/operator?level=repository",
        ),
        ("/classic/coverage?status=gap", "/coverage/operator?status=gap"),
        ("/classic/config-scan?product=7", "/config-scan/products/7"),
        ("/classic/iac?scan_error=invalid_target", "/iac/operator?scan_error=invalid_target"),
        ("/classic/ptai-scans?problem=true", "/ptai-scans?problem=true"),
        (
            "/classic/scanners?status=offline",
            "/scanners/operator",
        ),
        (
            "/classic/integrations?focus=gitlab&instance=7",
            "/integrations?instance=7&integration=gitlab",
        ),
        ("/classic/reports?year=2026&quarter=3", "/targets?level=product&period=2026-Q3&mode=new"),
    ],
)
def test_classic_workspace_aliases_only_redirect(path, destination):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == destination


@pytest.mark.needs_db
def test_target_operator_workspace_keeps_product_maintenance_reachable():
    db = SessionLocal()
    product = Product(
        name="QA product maintenance contract",
        path="qa/product-maintenance-contract",
        gitlab_base_url="https://gitlab.example.test",
    )
    db.add(product)
    db.commit()

    try:
        response = client.get("/targets/operator?level=product")

        assert response.status_code == 200
        assert 'action="/products/normalize-paths"' in response.text
        assert 'action="/products/cleanup-duplicates"' in response.text
        assert f'action="/products/{product.id}/delete"' in response.text
    finally:
        db.delete(product)
        db.commit()
        db.close()


@pytest.mark.needs_db
def test_dashboard_topology_live_payload():
    response = client.get("/dashboard/topology?timeframe=7d")
    assert response.status_code == 200
    payload = response.json()
    assert {
        "observed_steps",
        "target_count",
        "gitlab_triggers",
        "active_pipelines",
        "success_rate",
        "failure_rate",
        "execution_completed",
        "execution_failed",
        "execution_total",
        "execution_timeframe",
        "execution_timeframe_label",
        "pipeline_success_rate",
        "pipeline_failure_rate",
        "pipeline_completed",
        "pipeline_failed",
        "pipeline_total",
        "normalized_outputs",
        "failed_outputs",
        "config_drift",
        "asoc_mapped_products",
        "total_products",
        "last_execution_at",
        "last_execution_label",
        "telemetry_age_seconds",
        "telemetry_stale_after_seconds",
        "telemetry_state",
        "updated_at",
    } <= payload.keys()
    assert payload["normalized_outputs"] == payload["observed_steps"]


@pytest.mark.needs_db
def test_signal_control_overview_adapter_uses_real_view_model():
    response = client.get("/api/ui/overview?timeframe=30d")
    assert response.status_code == 200
    payload = response.json()
    assert payload.keys() == {
        "topology",
        "attention",
        "overview_signal",
        "hotspots",
        "posture",
        "platform",
        "throughput",
        "throughput_window_label",
        "throughput_timezone",
        "throughput_granularity",
        "latency",
        "duration_breakdown",
    }
    assert payload["attention"].keys() == {
        "failed_jobs",
        "failed_jobs_in_window",
        "config_drift",
        "config_products_scanned",
        "iac_critical_high",
        "iac_repositories_total",
        "iac_repositories_scanned",
        "coverage_gaps",
        "coverage_targets_total",
    }
    assert len(payload["throughput"]) == 31
    assert all(
        bucket.keys() == {
            "label",
            "range_label",
            "start_at",
            "end_at",
            "is_current",
            "succeeded",
            "failed",
        }
        for bucket in payload["throughput"]
    )
    assert payload["throughput_granularity"] == "day"
    assert payload["throughput_timezone"]
    assert payload["throughput_window_label"] == "Last 30 days · daily buckets"
    assert payload["latency"].keys() == {
        "sample_count",
        "average",
        "average_label",
        "median",
        "median_label",
        "p95",
        "p95_label",
        "p95_tail",
        "p95_tail_label",
    }
    assert payload["duration_breakdown"].keys() == {"pipeline", "categories", "jobs"}
    assert payload["duration_breakdown"]["pipeline"].keys() == {
        "count",
        "median",
        "median_label",
        "average",
        "average_label",
    }
    for row in payload["duration_breakdown"]["categories"] + payload["duration_breakdown"]["jobs"]:
        assert set(row) == {
            "key",
            "label",
            "count",
            "median",
            "median_label",
            "average",
            "average_label",
        }
    assert payload["overview_signal"].keys() == {
        "tone",
        "title",
        "context",
        "action_label",
        "action_href",
    }
    assert payload["posture"].keys() == {
        "audit_covered",
        "audit_targets",
        "audit_gaps",
        "audit_rate",
        "controls_expected",
        "controls_configured",
        "controls_missing",
        "control_rate",
        "iac_critical_high",
        "iac_repositories_scanned",
        "config_findings",
        "config_products_scanned",
        "asoc_mapped_products",
        "total_products",
    }
    assert payload["platform"].keys() == {
        "instances_total",
        "instances_healthy",
        "instances_problem",
        "problems",
    }
    assert all(
        set(row) == {
            "repository_id",
            "repository",
            "product",
            "job",
            "open_failures",
            "last_failure_at",
            "href",
        }
        for row in payload["hotspots"]
    )
    assert payload["topology"]["execution_timeframe"] == "30d"
    assert payload["topology"]["execution_timeframe_label"] == "Last 30 days"


@pytest.mark.needs_db
@pytest.mark.parametrize(
    ("timeframe", "bucket_count", "granularity"),
    [
        ("today", 24, "hour"),
        ("3d", 25, "3h"),
        ("7d", 8, "day"),
        ("1y", 13, "month"),
    ],
)
def test_signal_control_throughput_matches_selected_window(
    timeframe, bucket_count, granularity
):
    response = client.get(f"/api/ui/overview?timeframe={timeframe}")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["throughput"]) == bucket_count
    assert payload["throughput_granularity"] == granularity
    assert payload["throughput"][0]["start_at"]
    assert payload["throughput"][0]["range_label"].endswith(payload["throughput_timezone"])


def test_signal_control_throughput_aggregates_rolling_and_adaptive_three_day_buckets(monkeypatch):
    fixed_now = datetime(2026, 8, 1, 12, 34, tzinfo=timezone(timedelta(hours=3)))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(dashboard_router, "datetime", FrozenDateTime)
    captured = {}

    def fake_counts(_db, _service, start, _trunc, _category, *, timezone_name, end_at):
        captured["start"] = start
        return captured.get("counts", {})

    monkeypatch.setattr(dashboard_router, "_status_counts_by_bucket", fake_counts)

    rolling_cutoff = fixed_now - timedelta(hours=72)
    rolling_start = rolling_cutoff.replace(minute=0, second=0, microsecond=0)
    captured["counts"] = {
        (rolling_start + timedelta(hours=offset)).replace(tzinfo=None): {
            JobStatus.SUCCESS: 1,
            JobStatus.FAILED: 2,
        }
        for offset in (0, 1, 2)
    }
    rolling = dashboard_router._build_signal_throughput(object(), object(), "3d")
    assert rolling["buckets"][0]["start_at"] == rolling_start.isoformat()
    assert rolling["buckets"][0]["succeeded"] == 3
    assert rolling["buckets"][0]["failed"] == 6
    assert captured["start"] == rolling_cutoff.astimezone(timezone.utc)

    class EarliestQuery:
        def filter(self, *args, **kwargs):
            return self

        def scalar(self):
            return fixed_now - timedelta(days=45)

    class FakeDB:
        def query(self, *args, **kwargs):
            return EarliestQuery()

    adaptive_start = (fixed_now - timedelta(days=45)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    captured["counts"] = {
        (adaptive_start + timedelta(days=offset)).replace(tzinfo=None): {
            JobStatus.SUCCESS: offset + 1,
            JobStatus.FAILED: 1,
        }
        for offset in (0, 1, 2)
    }
    adaptive = dashboard_router._build_signal_throughput(FakeDB(), object(), "all")
    assert adaptive["granularity"] == "3d"
    assert adaptive["buckets"][0]["succeeded"] == 6
    assert adaptive["buckets"][0]["failed"] == 3


@pytest.mark.needs_db
def test_signal_control_overview_defaults_and_normalizes_timeframe():
    default_payload = client.get("/api/ui/overview").json()
    invalid_payload = client.get("/api/ui/overview?timeframe=invalid").json()
    all_time_payload = client.get("/api/ui/overview?timeframe=all").json()

    assert default_payload["topology"]["execution_timeframe"] == "7d"
    assert invalid_payload["topology"]["execution_timeframe"] == "7d"
    assert all_time_payload["topology"]["execution_timeframe"] == "all"
    assert all_time_payload["topology"]["execution_timeframe_label"] == "All time"


@pytest.mark.needs_db
@pytest.mark.parametrize(
    ("endpoint", "required_keys"),
    [
        (
            "/api/ui/executions",
            {"rows", "counts", "filter", "matching", "pagination"},
        ),
        ("/api/ui/failed-jobs", {"rows", "summary"}),
        (
            "/api/ui/job-journal?status=failed",
            {"rows", "summary", "pagination", "filters"},
        ),
        ("/api/ui/pipelines", {"rows", "counts", "total", "matching"}),
        (
            "/api/ui/config-guard",
            {"runs", "findings", "severity", "active", "pagination"},
        ),
        ("/api/ui/iac", {"runs", "repositories", "findings"}),
        ("/api/ui/ptai", {"rows"}),
        ("/api/ui/coverage", {"summary", "rows"}),
        ("/api/ui/products", {"rows", "summary", "periods", "sync", "unregistered_groups"}),
        (
            "/api/ui/repositories",
            {
                "rows",
                "total",
                "matching",
                "returned",
                "pagination",
                "filters",
            },
        ),
        ("/api/ui/scanner-fleet", {"connectors", "instances", "workers"}),
        ("/api/ui/integrations", {"rows"}),
    ],
)
def test_signal_control_live_adapters(endpoint, required_keys):
    response = client.get(endpoint)
    assert response.status_code == 200
    assert required_keys <= response.json().keys()


@pytest.mark.needs_db
def test_signal_control_pipeline_filters_use_the_existing_query_contract():
    response = client.get(
        "/api/ui/pipelines"
        "?group=needs_attention&status=failed&timeframe=30d&page=999"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["group"] == "needs_attention"
    assert payload["status"] == "failed"
    assert payload["timeframe"] == "30d"
    assert payload["page"] <= payload["page_count"]


@pytest.mark.needs_db
def test_signal_control_ptai_filters_and_pagination_use_the_existing_contract():
    response = client.get(
        "/api/ui/ptai"
        "?status=error&repo=scanops&branch=main&problem=true&page=999&page_size=100"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["filters"] == {
        "status": "ERROR",
        "repo": "scanops",
        "branch": "main",
        "problem": True,
    }
    assert payload["pagination"]["page"] <= payload["pagination"]["total_pages"]
    assert payload["pagination"]["page_size"] == 100


def test_signal_control_paginated_views_reject_unbounded_page_sizes():
    for endpoint in ("executions", "config-guard", "ptai"):
        response = client.get(f"/api/ui/{endpoint}?page_size=1000")
        assert response.status_code == 422


@pytest.mark.needs_db
def test_signal_control_repository_360_adapter():
    repositories = client.get("/api/ui/repositories").json()["rows"]
    if not repositories:
        pytest.skip("no repository available for Target 360 contract")

    response = client.get(
        f"/api/ui/repositories/{repositories[0]['id']}"
    )
    assert response.status_code == 200
    payload = response.json()
    assert {
        "repository",
        "product",
        "coverage",
        "controls",
        "executions",
        "summary",
        "freshness",
        "versions",
    } <= payload.keys()


@pytest.mark.needs_db
def test_signal_control_repository_inventory_has_server_side_filters_and_pages():
    response = client.get(
        "/api/ui/repositories",
        params={"page": 999, "page_size": 25},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["returned"] == len(payload["rows"])
    assert payload["returned"] <= 25
    assert payload["matching"] == payload["total"]
    assert payload["pagination"]["page_size"] == 25
    assert payload["pagination"]["page"] <= payload["pagination"]["total_pages"]

    if not payload["rows"]:
        pytest.skip("no repository available for inventory filter contract")

    repository = payload["rows"][0]
    assert {
        "job_health_healthy",
        "job_health_broken",
        "job_health_total",
    } <= repository.keys()
    assert repository["job_health_total"] == (
        repository["job_health_healthy"] + repository["job_health_broken"]
    )
    assert repository["success_rate"] == (
        round(
            repository["job_health_healthy"]
            / repository["job_health_total"]
            * 100,
            2,
        )
        if repository["job_health_total"]
        else 0
    )
    query_text = repository["path"].split("/")[-1]
    filtered = client.get(
        "/api/ui/repositories",
        params={
            "q": query_text,
            "product": repository["product_id"],
            "page_size": 100,
        },
    )
    assert filtered.status_code == 200
    filtered_payload = filtered.json()
    assert filtered_payload["filters"] == {
        "q": query_text,
        "product": repository["product_id"],
    }
    assert filtered_payload["returned"] == len(filtered_payload["rows"])
    assert filtered_payload["matching"] <= filtered_payload["total"]
    assert filtered_payload["pagination"]["page_size"] == 100
    assert all(
        row["product_id"] == repository["product_id"]
        and (
            query_text.casefold() in row["path"].casefold()
            or query_text.casefold() in (row["product"] or "").casefold()
        )
        for row in filtered_payload["rows"]
    )


def test_signal_control_repository_inventory_rejects_unbounded_page_sizes():
    response = client.get("/api/ui/repositories?page_size=1000")
    assert response.status_code == 422


@pytest.mark.needs_db
def test_signal_control_repository_inventory_keeps_the_bounded_legacy_limit_alias():
    response = client.get("/api/ui/repositories?limit=1000")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["page_size"] == 1000
    assert payload["returned"] == len(payload["rows"])
    assert payload["returned"] <= 1000
    assert payload["matching"] == payload["total"]


@pytest.mark.needs_db
def test_signal_control_overview_failure_count_matches_queue():
    overview = client.get("/api/ui/overview")
    failures = client.get("/api/ui/failed-jobs")

    assert overview.status_code == 200
    assert failures.status_code == 200
    assert (
        overview.json()["attention"]["failed_jobs"]
        == failures.json()["summary"]["open"]
    )


@pytest.mark.needs_db
def test_products_page():
    """Test products page loads"""
    response = client.get("/products")
    assert response.status_code == 200

@pytest.mark.needs_db
def test_logs_page():
    """The canonical logs route loads the Signal Control journal shell."""
    response = client.get("/logs?status=failed")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text
    assert "ScanOps — Signal Control" in response.text


def test_next_failed_jobs_alias_redirects_to_canonical_logs_route():
    response = client.get(
        "/next?view=failed&repo=group%2Frepo&timeframe=30d",
        follow_redirects=False,
    )

    assert response.status_code == 308
    assert response.headers["location"] == (
        "/logs?repo=group%2Frepo&timeframe=30d&status=failed"
    )


def test_next_executions_alias_redirects_to_canonical_executions_route():
    response = client.get(
        "/next?view=executions&repository=17",
        follow_redirects=False,
    )

    assert response.status_code == 308
    assert response.headers["location"] == "/executions?repository=17"


@pytest.mark.parametrize(
    ("view", "location"),
    [
        ("overview", "/"),
        ("jobs", "/logs"),
        ("pipelines", "/pipelines"),
        ("config", "/config-scan"),
        ("iac", "/iac"),
        ("ptai", "/ptai-scans"),
        ("coverage", "/coverage"),
        ("products", "/targets?level=product"),
        ("repositories", "/targets?level=repository"),
        ("reports", "/targets?level=product"),
        ("scanners", "/scanners"),
        ("integrations", "/integrations"),
    ],
)
def test_next_aliases_redirect_to_canonical_product_routes(view, location):
    response = client.get(f"/next?view={view}", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == location


def test_failed_jobs_legacy_url_redirects_to_jobs_status_filter():
    response = client.get(
        "/failed?repo=group%2Frepo&category=sast&timeframe=30d",
        follow_redirects=False,
    )

    assert response.status_code == 308
    assert response.headers["location"] == (
        "/logs?repo=group%2Frepo&category=sast&timeframe=30d&status=failed"
    )

@pytest.mark.needs_db
def test_audit_page():
    """Test audit page loads"""
    response = client.get("/audit")
    assert response.status_code == 200

@pytest.mark.needs_db
def test_config_scan_page():
    """Test config scan overview loads"""
    response = client.get("/config-scan")
    assert response.status_code == 200


@pytest.mark.needs_db
def test_iac_page():
    response = client.get("/iac")
    assert response.status_code == 200


@pytest.mark.needs_db
def test_iac_runs_page():
    response = client.get("/iac/runs")
    assert response.status_code == 200


@pytest.mark.needs_db
def test_iac_repositories_page():
    response = client.get("/iac/repositories")
    assert response.status_code == 200


@pytest.mark.needs_db
def test_dashboard_duration_page():
    response = client.get("/dashboard/duration")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text


@pytest.mark.needs_db
def test_signal_control_duration_api_contract():
    response = client.get("/api/ui/duration?timeframe=7d")
    assert response.status_code == 200
    payload = response.json()
    assert payload.keys() == {
        "timeframe",
        "pipeline",
        "jobs",
        "categories",
        "job_breakdown",
        "trend",
    }
    assert payload["timeframe"] == "7d"
    assert payload["pipeline"].keys() == {
        "count",
        "average",
        "average_label",
        "median",
        "median_label",
        "p95",
        "p95_label",
    }
    assert payload["jobs"].keys() == payload["pipeline"].keys()
    assert payload["trend"] is None or payload["trend"].keys() == {
        "mode",
        "labels",
        "display_labels",
        "pipeline_avg",
        "job_avg",
        "pipeline_count",
        "job_count",
    }


def test_api_docs():
    """Test API documentation is accessible"""
    response = client.get("/docs")
    assert response.status_code == 200


def test_product_namespace_depth_is_explicit_configuration(monkeypatch):
    monkeypatch.delenv("PRODUCT_NESTED_NAMESPACES", raising=False)
    path = "sandbox/orders/api"
    assert DatabaseService._derive_product_identity(path) == ("sandbox", "sandbox")
    assert AuditService._normalize_scope_from_product_path(path) == "sandbox"
    assert _normalize_product_group_path(path, "fallback") == "sandbox"
    monkeypatch.setenv("PRODUCT_NESTED_NAMESPACES", "sandbox,example-suite")
    assert DatabaseService._derive_product_identity(path) == ("orders", "sandbox/orders")
    assert AuditService._normalize_scope_from_product_path(path) == "sandbox/orders"
    assert _normalize_product_group_path(path, "fallback") == "sandbox/orders"
