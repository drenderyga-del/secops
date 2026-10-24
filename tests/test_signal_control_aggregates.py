from datetime import datetime, timedelta, timezone
from statistics import median
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, or_

from app.database import DatabaseService
from app.main import app
from app.models import (
    ConfigScanFinding,
    ConfigScanRun,
    Delivery,
    Execution,
    IacScanRun,
    JobReport,
    PtaiScan,
    SessionLocal,
)
from app.models.job_report import JobStatus
from app.pipeline_query_service import PipelineQueryService
from app.routers.web import signal_control as signal_control_router
from app.routers.web.signal_control import (
    _iac_run_summary,
    _median,
    _ptai_scan_is_active,
    _scanner_fleet_summary,
    _signal_control_direction,
    _worker_freshness,
)


client = TestClient(app)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], None),
        ([7], 7.0),
        ([9, 1, 5], 5.0),
        ([10, 2, 8, 4], 6.0),
    ],
)
def test_failed_queue_median_has_even_and_odd_semantics(values, expected):
    assert _median(values) == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        ({"finished_at": datetime(2026, 7, 28, tzinfo=timezone.utc)}, False),
        ({"trigger_failed": True}, False),
        ({"trigger_failed": None}, False),
        ({"asoc_status": "SUCCESS"}, False),
        ({"asoc_status": "ERROR"}, False),
        ({"asoc_status": "FAILED"}, False),
        ({"ptai_stage": "Done"}, False),
        ({"ptai_stage": "Failed"}, False),
    ],
)
def test_ptai_active_predicate_handles_finished_and_legacy_terminal_rows(
    overrides,
    expected,
):
    values = {
        "finished_at": None,
        "trigger_failed": False,
        "asoc_status": "RUNNING",
        "ptai_stage": "Scanning",
    }
    values.update(overrides)
    assert _ptai_scan_is_active(SimpleNamespace(**values)) is expected


def test_worker_freshness_uses_configured_threshold_and_handles_naive_time():
    now = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)

    assert _worker_freshness(
        now - timedelta(seconds=900),
        now=now,
        stale_after_seconds=900,
    ) == {
        "status": "healthy",
        "age_seconds": 900,
        "stale": False,
    }
    assert _worker_freshness(
        (now - timedelta(seconds=901)).replace(tzinfo=None),
        now=now,
        stale_after_seconds=900,
    ) == {
        "status": "stale",
        "age_seconds": 901,
        "stale": True,
    }


def test_iac_summary_uses_one_run_scoped_finding_source():
    summary = _iac_run_summary(
        4,
        ["critical", "HIGH", "high", "medium", "low", "unknown"],
    )

    assert summary == {
        "repositories": 4,
        "findings": 6,
        "critical": 1,
        "high": 2,
        "medium": 1,
        "low": 1,
        "info": 1,
    }
    assert summary["findings"] == sum(
        summary[key]
        for key in ("critical", "high", "medium", "low", "info")
    )


def test_scanner_fleet_summary_names_total_returned_and_healthy_instances():
    instances = [
        SimpleNamespace(health_status="healthy"),
        SimpleNamespace(health_status="success"),
        SimpleNamespace(health_status="degraded"),
        SimpleNamespace(health_status=None),
    ]

    assert _scanner_fleet_summary(instances, total=9) == {
        "source": "scanner_instances",
        "total": 9,
        "returned": 4,
        "healthy": 2,
    }


@pytest.mark.needs_db
def test_scanner_fleet_refreshes_connector_projection_before_read(monkeypatch):
    calls = []

    def record_refresh(db):
        calls.append(db)
        return {"connectors": [], "instances": []}

    monkeypatch.setattr(signal_control_router, "refresh_registry", record_refresh)

    response = client.get("/api/ui/scanner-fleet")

    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("canonical", "normalized"),
    [
        ("inbound", "inbound"),
        ("outbound", "outbound"),
        ("bidirectional", "both"),
        ("both", "both"),
    ],
)
def test_signal_control_direction_normalizes_without_mutating_canonical_value(
    canonical,
    normalized,
):
    assert _signal_control_direction(canonical) == normalized


@pytest.mark.needs_db
def test_signal_control_additive_aggregate_contracts():
    registry_response = client.get("/api/ui/products")
    assert registry_response.status_code == 200
    registry = registry_response.json()
    summary = registry["summary"]
    assert len(registry["rows"]) == summary["total"]
    assert summary["total"] == sum(
        summary[key] for key in ("synced", "attention", "removed")
    )

    iac_response = client.get("/api/ui/iac")
    assert iac_response.status_code == 200
    iac_payload = iac_response.json()
    assert {"active_run_id", "latest_run_id", "selected_run_id"} <= iac_payload.keys()
    if iac_payload["summary"] is not None:
        assert iac_payload["selected_run_id"] is not None
        assert {
            row["run_id"] for row in iac_payload["repositories"]
        } <= {iac_payload["selected_run_id"]}
        assert {
            row["run_id"] for row in iac_payload["findings"]
        } <= {iac_payload["selected_run_id"]}
        assert iac_payload["summary"].keys() == {
            "repositories",
            "findings",
            "critical",
            "high",
            "medium",
            "low",
            "info",
        }
        assert iac_payload["summary"]["findings"] == sum(
            iac_payload["summary"][key]
            for key in ("critical", "high", "medium", "low", "info")
        )

    config_response = client.get("/api/ui/config-guard")
    assert config_response.status_code == 200
    config_payload = config_response.json()
    assert {"active_run_id", "selected_run_id"} <= config_payload.keys()
    assert config_payload["summary"].keys() == {
        "products_scanned",
        "open_findings",
        "retained_findings",
    }
    assert config_payload["summary"]["retained_findings"] >= len(
        config_payload["findings"]
    )
    assert config_payload["pagination"]["end"] <= config_payload["summary"][
        "retained_findings"
    ]
    if config_payload["selected_finding"] is not None:
        assert (
            config_payload["selected_finding"]["run_id"]
            == config_payload["selected_run_id"]
        )

    scanner_response = client.get("/api/ui/scanner-fleet")
    assert scanner_response.status_code == 200
    scanner_payload = scanner_response.json()
    assert scanner_payload["summary"] == {
        "source": "scanner_instances",
        "total": len(scanner_payload["instances"]),
        "returned": len(scanner_payload["instances"]),
        "healthy": sum(
            item["status"] in {"healthy", "success"}
            for item in scanner_payload["instances"]
        ),
    }
    for worker in scanner_payload["workers"]:
        assert worker["status"] in {"healthy", "stale"}
        assert worker["stale"] is (worker["status"] == "stale")
        assert worker["age_seconds"] is None or worker["age_seconds"] >= 0
    workers_by_name = {
        worker["name"]: worker for worker in scanner_payload["workers"]
    }
    for instance in scanner_payload["instances"]:
        if instance["type"] != "worker":
            continue
        worker_name = instance["key"].removeprefix("worker:")
        assert instance["status"] == workers_by_name[worker_name]["status"]
        assert instance["last_seen_at"] == workers_by_name[worker_name]["last_beat_at"]

    integrations_response = client.get("/api/ui/integrations")
    assert integrations_response.status_code == 200
    integrations_payload = integrations_response.json()
    assert integrations_payload["management"].keys() == {
        "available",
        "auth_ready",
        "credential_store_ready",
    }
    assert integrations_payload["summary"]["integration_types"] == len(
        integrations_payload["rows"]
    )
    assert integrations_payload["summary"]["connections"] == sum(
        row["connection_summary"]["total"]
        for row in integrations_payload["rows"]
    )
    for key in ("configured", "healthy", "failed", "untested", "disabled"):
        assert integrations_payload["summary"][key] == sum(
            row["connection_summary"][key]
            for row in integrations_payload["rows"]
        )
    for row in integrations_payload["rows"]:
        assert row["direction"] in {"inbound", "outbound", "both"}
        if row["canonical_direction"] == "bidirectional":
            assert row["direction"] == "both"
        else:
            assert row["direction"] == row["canonical_direction"]
        assert row["health_status"] in {
            "healthy",
            "failed",
            "partial",
            "disabled",
            "unknown",
        }
        assert row["href"].startswith("/integrations?integration=")
        for field in row["fields"]:
            if field["secret"]:
                assert field["value"] == ""
        for instance in row["instances"]:
            assert {
                "token",
                "password",
                "secret",
                "private_key",
            }.isdisjoint(instance)
        if row["key"] == "gitlab" and any(
            instance["enabled"] and instance["test_status"] == "failed"
            for instance in row["instances"]
        ):
            assert row["health_status"] == "failed"


@pytest.mark.needs_db
def test_asoc_delivery_topology_excludes_non_asoc_deliveries():
    asoc_filter = or_(
        Delivery.destination == "asoc",
        Delivery.destination.like("asoc:%"),
    )

    topology = client.get("/dashboard/topology?timeframe=1y")
    assert topology.status_code == 200
    cutoff = DatabaseService._resolve_timeframe_cutoff("1y")
    with SessionLocal() as db:
        expected_confirmed = (
            db.query(Delivery)
            .filter(
                asoc_filter,
                Delivery.status == "confirmed",
                func.coalesce(
                    Delivery.confirmed_at,
                    Delivery.attempted_at,
                    Delivery.created_at,
                )
                >= cutoff,
            )
            .count()
        )
    assert (
        topology.json()["asoc_confirmed_deliveries"]
        == expected_confirmed
    )


@pytest.mark.needs_db
def test_overview_success_rate_uses_terminal_jobs_not_pipeline_outcomes():
    response = client.get("/api/ui/overview?timeframe=today")
    assert response.status_code == 200
    topology = response.json()["topology"]
    cutoff = DatabaseService._resolve_timeframe_cutoff("today")

    with SessionLocal() as db:
        counts = {
            status: int(count)
            for status, count in db.query(
                JobReport.job_status,
                func.count(JobReport.id),
            )
            .filter(
                JobReport.finished_at >= cutoff,
                JobReport.job_status.in_(
                    [JobStatus.SUCCESS, JobStatus.FAILED]
                ),
            )
            .group_by(JobReport.job_status)
            .all()
        }
        pipeline = PipelineQueryService(db).execution_health("today")

    successful = counts.get(JobStatus.SUCCESS, 0)
    failed = counts.get(JobStatus.FAILED, 0)
    terminal = successful + failed

    assert topology["execution_completed"] == successful
    assert topology["execution_failed"] == failed
    assert topology["execution_total"] == terminal
    assert topology["success_rate"] == (
        round((successful / terminal) * 100, 1) if terminal else 0
    )
    assert topology["pipeline_completed"] == pipeline.completed
    assert topology["pipeline_failed"] == pipeline.failed
    assert topology["pipeline_total"] == pipeline.total
    assert topology["pipeline_success_rate"] == pipeline.success_rate


@pytest.mark.needs_db
def test_execution_repository_filter_applies_to_rows_and_status_counts():
    unfiltered = client.get("/api/ui/executions?limit=200")
    assert unfiltered.status_code == 200
    repository_id = next(
        (
            row["repository_id"]
            for row in unfiltered.json()["rows"]
            if row["repository_id"] is not None
        ),
        None,
    )
    if repository_id is None:
        pytest.skip("no repository-scoped execution available")

    response = client.get(
        f"/api/ui/executions?repository={repository_id}&limit=200"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["repository_filter"] == repository_id
    assert all(
        row["repository_id"] == repository_id for row in payload["rows"]
    )

    with SessionLocal() as db:
        expected_counts = dict(
            db.query(Execution.status, func.count(Execution.id))
            .filter(Execution.repository_id == repository_id)
            .group_by(Execution.status)
            .all()
        )
    assert payload["counts"] == {
        "all": sum(expected_counts.values()),
        **{key: int(value) for key, value in expected_counts.items()},
    }


@pytest.mark.needs_db
def test_failed_job_summary_uses_full_unbounded_queue():
    response = client.get("/api/ui/failed-jobs?limit=1")
    assert response.status_code == 200
    summary = response.json()["summary"]

    with SessionLocal() as db:
        queue_rows = (
            DatabaseService(db)
            ._active_failed_jobs_query()
            .with_entities(JobReport.duration_seconds, JobReport.finished_at)
            .all()
        )
    durations = [
        float(duration)
        for duration, _ in queue_rows
        if duration is not None
    ]
    assert summary["open"] == len(queue_rows)
    assert summary["returned"] == min(len(queue_rows), 1)
    assert summary["median_duration_seconds"] == (
        median(durations) if durations else None
    )
    oldest_finished_at = min(
        (finished_at for _, finished_at in queue_rows if finished_at is not None),
        default=None,
    )
    if oldest_finished_at is None:
        assert summary["oldest_age_seconds"] is None
    else:
        if oldest_finished_at.tzinfo is None:
            oldest_finished_at = oldest_finished_at.replace(tzinfo=timezone.utc)
        expected_age = max(
            int(
                (
                    datetime.now(timezone.utc) - oldest_finished_at
                ).total_seconds()
            ),
            0,
        )
        assert abs(summary["oldest_age_seconds"] - expected_age) <= 5
    if len(queue_rows) > 1:
        assert summary["open"] > summary["returned"]


@pytest.mark.needs_db
def test_job_journal_preserves_failed_filter_and_pagination_contract():
    response = client.get(
        "/api/ui/job-journal"
        "?status=failed&category=all&timeframe=7d&page=1&page_size=5"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filters"]["status"] == "failed"
    assert payload["filters"]["category"] == "all"
    assert payload["filters"]["timeframe"] == "7d"
    assert payload["pagination"]["page"] == 1
    assert payload["pagination"]["page_size"] == 5
    assert payload["summary"]["matching"] >= len(payload["rows"])
    assert payload["summary"]["returned"] == len(payload["rows"])
    assert payload["summary"]["successful"] == 0
    assert payload["summary"]["failed"] == payload["summary"]["terminal"]
    assert payload["summary"]["success_rate"] in {0, None}
    assert len(payload["rows"]) <= 5
    assert all(row["status"] == "failed" for row in payload["rows"])

    with SessionLocal() as db:
        query = DatabaseService(db).get_jobs_query(
            repo_filter=None,
            job_filter=None,
            category_filter=None,
            status_filter="failed",
            only_failed=True,
            timeframe="7d",
        )
        duration_rows = [
            float(value)
            for value, in query.filter(
                JobReport.duration_seconds.isnot(None)
            )
            .with_entities(JobReport.duration_seconds)
            .all()
        ]
        oldest_finished_at = query.with_entities(
            func.min(JobReport.finished_at)
        ).scalar()

    assert payload["summary"]["duration_samples"] == len(duration_rows)
    assert payload["summary"]["median_duration_seconds"] == (
        median(duration_rows) if duration_rows else None
    )
    assert payload["summary"]["oldest_finished_at"] == (
        oldest_finished_at.isoformat() if oldest_finished_at else None
    )


@pytest.mark.needs_db
def test_job_journal_exposes_raw_terminal_job_outcomes_for_the_current_scope():
    response = client.get(
        "/api/ui/job-journal?status=all&timeframe=all&page_size=1"
    )
    assert response.status_code == 200
    summary = response.json()["summary"]

    with SessionLocal() as db:
        counts = {
            status: int(count)
            for status, count in db.query(
                JobReport.job_status,
                func.count(JobReport.id),
            )
            .filter(
                JobReport.job_status.in_(
                    [JobStatus.SUCCESS, JobStatus.FAILED]
                )
            )
            .group_by(JobReport.job_status)
            .all()
        }
    successful = counts.get(JobStatus.SUCCESS, 0)
    failed = counts.get(JobStatus.FAILED, 0)
    terminal = successful + failed

    assert summary["successful"] == successful
    assert summary["failed"] == failed
    assert summary["terminal"] == terminal
    assert summary["success_rate"] == (
        round((successful / terminal) * 100, 1) if terminal else None
    )


@pytest.mark.needs_db
def test_job_journal_does_not_describe_active_jobs_as_terminal():
    response = client.get(
        "/api/ui/job-journal?status=all&timeframe=all&page_size=100"
    )
    assert response.status_code == 200

    for row in response.json()["rows"]:
        if row["status"] in {"running", "pending"}:
            assert "terminal status" not in row["detail"]
            assert "last reported with status" in row["detail"]


@pytest.mark.needs_db
def test_config_guard_active_count_is_not_limited_to_returned_runs():
    response = client.get("/api/ui/config-guard")
    assert response.status_code == 200
    with SessionLocal() as db:
        order = (ConfigScanRun.created_at.desc(), ConfigScanRun.id.desc())
        expected = int(
            db.query(func.count(ConfigScanRun.id))
            .filter(ConfigScanRun.status.in_(("queued", "running")))
            .scalar()
            or 0
        )
        expected_active_run = (
            db.query(ConfigScanRun)
            .filter(ConfigScanRun.status.in_(("queued", "running")))
            .order_by(*order)
            .first()
        )
        expected_selected_run = (
            db.query(ConfigScanRun)
            .filter(
                ConfigScanRun.status.in_(
                    ("succeeded", "failed", "cancelled")
                )
            )
            .order_by(*order)
            .first()
        ) or expected_active_run
    payload = response.json()
    assert payload["active"] == expected
    assert payload["active_run_id"] == (
        expected_active_run.id if expected_active_run else None
    )
    assert payload["selected_run_id"] == (
        expected_selected_run.id if expected_selected_run else None
    )


@pytest.mark.needs_db
def test_config_guard_normalizes_severity_counts_across_pages():
    marker = "qa-config-severity-case-pagination"
    severity_variants = ("Qa_MiXeD", "qa_mixed")

    with SessionLocal() as db:
        run = db.query(ConfigScanRun).order_by(ConfigScanRun.id.desc()).first()
        if run is None:
            pytest.skip("Config Guard fixture is unavailable")

        existing_count = int(
            db.query(func.count(ConfigScanFinding.id))
            .filter(func.lower(ConfigScanFinding.severity) == "qa_mixed")
            .scalar()
            or 0
        )
        findings = [
            ConfigScanFinding(
                run_id=run.id,
                product_id=run.product_id,
                project_path=f"{marker}-{index:02d}",
                severity=severity_variants[index % len(severity_variants)],
                rule_id=f"QA-CASE-{index:02d}",
                title=f"Mixed-case severity finding {index}",
            )
            for index in range(26)
        ]
        db.add_all(findings)
        db.commit()

    try:
        first_response = client.get(
            "/api/ui/config-guard", params={"page": 1, "page_size": 25}
        )
        second_response = client.get(
            "/api/ui/config-guard", params={"page": 2, "page_size": 25}
        )
        assert first_response.status_code == 200
        assert second_response.status_code == 200

        first_payload = first_response.json()
        second_payload = second_response.json()
        assert first_payload["severity"]["qa_mixed"] == existing_count + 26
        assert second_payload["severity"]["qa_mixed"] == existing_count + 26

        returned_marker_rows = [
            row
            for row in [
                *first_payload["findings"],
                *second_payload["findings"],
            ]
            if row["project_path"].startswith(marker)
        ]
        assert len(returned_marker_rows) == 26
        assert first_payload["pagination"]["total_pages"] >= 2
    finally:
        with SessionLocal() as db:
            db.query(ConfigScanFinding).filter(
                ConfigScanFinding.project_path.like(f"{marker}%")
            ).delete(synchronize_session=False)
            db.commit()


@pytest.mark.needs_db
def test_ptai_summary_and_rows_share_active_predicate():
    response = client.get("/api/ui/ptai")
    assert response.status_code == 200
    payload = response.json()
    with SessionLocal() as db:
        all_scans = db.query(PtaiScan).all()
    expected_active = sum(_ptai_scan_is_active(scan) for scan in all_scans)
    assert payload["summary"] == {
        "total": len(all_scans),
        "returned": len(payload["rows"]),
        "active": expected_active,
    }
    assert all(
        row["is_active"]
        == _ptai_scan_is_active(
            SimpleNamespace(
                finished_at=(
                    datetime.fromisoformat(row["finished_at"])
                    if row["finished_at"]
                    else None
                ),
                trigger_failed=row["trigger_failed"],
                asoc_status=row["status"],
                ptai_stage=row["ptai_stage"],
            )
        )
        for row in payload["rows"]
    )
    assert all(
        {"job_web_url", "asoc_repository_url", "ptai_result_url"} <= row.keys()
        for row in payload["rows"]
    )


@pytest.mark.needs_db
def test_ptai_pages_enumerate_equal_timestamp_scans_exactly_once():
    equal_created_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    active_created_at = datetime(2098, 1, 1, tzinfo=timezone.utc)

    with SessionLocal() as db:
        first_job_id = int(
            db.query(func.max(PtaiScan.gitlab_job_id)).scalar() or 0
        ) + 1
        marker = f"ptai-pagination-{first_job_id}"
        completed = [
            PtaiScan(
                gitlab_project_id=first_job_id,
                gitlab_pipeline_id=first_job_id,
                gitlab_job_id=first_job_id + index,
                repo_slug=marker,
                branch=marker,
                asoc_status="SUCCESS",
                trigger_failed=False,
                ptai_stage="Done",
                finished_at=equal_created_at,
                created_at=equal_created_at,
            )
            for index in range(53)
        ]
        active = PtaiScan(
            gitlab_project_id=first_job_id,
            gitlab_pipeline_id=first_job_id,
            gitlab_job_id=first_job_id + len(completed),
            repo_slug=marker,
            branch=marker,
            asoc_status="IN_PROGRESS",
            trigger_failed=False,
            ptai_stage="Scan",
            created_at=active_created_at,
        )
        db.add_all([*completed, active])
        db.commit()
        expected_ids = [
            row_id
            for row_id, in (
                db.query(PtaiScan.id)
                .order_by(PtaiScan.created_at.desc(), PtaiScan.id.desc())
                .all()
            )
        ]

    try:
        first_response = client.get("/api/ui/ptai", params={"page": 1})
        assert first_response.status_code == 200
        first_payload = first_response.json()
        page_ids = [row["id"] for row in first_payload["rows"]]

        for page in range(2, first_payload["pagination"]["total_pages"] + 1):
            response = client.get("/api/ui/ptai", params={"page": page})
            assert response.status_code == 200
            page_ids.extend(row["id"] for row in response.json()["rows"])

        assert page_ids == expected_ids
        assert len(page_ids) == len(set(page_ids))
        assert len(page_ids) == first_payload["matching"]
    finally:
        with SessionLocal() as db:
            db.query(PtaiScan).filter(PtaiScan.repo_slug == marker).delete(
                synchronize_session=False
            )
            db.commit()


@pytest.mark.needs_db
def test_iac_selects_latest_completed_or_latest_active_run():
    response = client.get("/api/ui/iac")
    assert response.status_code == 200
    payload = response.json()
    with SessionLocal() as db:
        order = (IacScanRun.created_at.desc(), IacScanRun.id.desc())
        expected = (
            db.query(IacScanRun)
            .filter(
                IacScanRun.status.in_(
                    ("succeeded", "partial", "failed")
                )
            )
            .order_by(*order)
            .first()
        )
        if expected is None:
            expected = db.query(IacScanRun).order_by(*order).first()
        expected_active = (
            db.query(IacScanRun)
            .filter(
                IacScanRun.status.in_(
                    (
                        "queued",
                        "requested",
                        "registered",
                        "running",
                        "polling",
                        "processing",
                        "in_progress",
                    )
                )
            )
            .order_by(*order)
            .first()
        )
        expected_latest = db.query(IacScanRun).order_by(*order).first()
    assert payload["selected_run_id"] == (
        expected.id if expected is not None else None
    )
    assert payload["active_run_id"] == (
        expected_active.id if expected_active is not None else None
    )
    assert payload["latest_run_id"] == (
        expected_latest.id if expected_latest is not None else None
    )
    if expected is not None:
        assert {
            row["run_id"] for row in payload["repositories"]
        } <= {expected.id}
        assert {
            row["run_id"] for row in payload["findings"]
        } <= {expected.id}
        severity_rank = {
            "critical": 0,
            "high": 1,
            "medium": 2,
            "low": 3,
            "info": 4,
        }
        ranks = [
            severity_rank.get(row["severity"], 4)
            for row in payload["findings"]
        ]
        assert ranks == sorted(ranks)


@pytest.mark.needs_db
@pytest.mark.parametrize(
    ("status", "step_states", "artifact_delivery"),
    [
        ("requested", [], None),
        ("queued", [], None),
        (
            "running",
            [("trigger", "succeeded"), ("scan", "running")],
            None,
        ),
        (
            "delivering",
            [
                ("trigger", "succeeded"),
                ("scan", "succeeded"),
                ("normalize", "succeeded"),
                ("deliver", "running"),
            ],
            "running",
        ),
        (
            "succeeded",
            [
                ("trigger", "succeeded"),
                ("scan", "succeeded"),
                ("normalize", "succeeded"),
                ("deliver", "succeeded"),
            ],
            "confirmed",
        ),
        ("failed", [("trigger", "succeeded"), ("scan", "failed")], None),
        (
            "cancelled",
            [("trigger", "succeeded"), ("scan", "cancelled")],
            None,
        ),
    ],
)
def test_seeded_execution_projection_keeps_lifecycle_and_evidence_coherent(
    status,
    step_states,
    artifact_delivery,
):
    db = SessionLocal()
    seeded_execution = (
        db.query(Execution)
        .filter(
            Execution.status == status,
            Execution.correlation_key.like("qa-execution-%"),
        )
        .order_by(Execution.id.asc())
        .first()
    )
    seeded_execution_id = seeded_execution.id if seeded_execution else None
    db.close()
    if seeded_execution_id is None:
        pytest.skip(f"database does not contain a seeded {status} execution")

    response = client.get(
        "/api/ui/executions",
        params={"status": status, "limit": 200},
    )
    assert response.status_code == 200
    rows = response.json()["rows"]
    row = next(item for item in rows if item["id"] == seeded_execution_id)
    assert [(step["name"], step["status"]) for step in row["steps"]] == step_states
    if status in {"requested", "queued"}:
        assert row["started_at"] is None
    if artifact_delivery is None:
        assert row["artifacts"] == []
    else:
        assert len(row["artifacts"]) == 1
        assert [
            delivery["status"]
            for delivery in row["artifacts"][0]["deliveries"]
        ] == [artifact_delivery]


@pytest.mark.needs_db
def test_seeded_partial_execution_matches_its_delivery_outcome():
    response = client.get(
        "/api/ui/executions",
        params={"status": "partial", "limit": 20},
    )
    assert response.status_code == 200
    rows = response.json()["rows"]
    if not rows:
        pytest.skip("database does not contain seeded partial executions")
    for row in rows:
        assert [step["name"] for step in row["steps"]] == [
            "trigger",
            "scan",
            "normalize",
            "deliver",
        ]
        delivery_status = row["artifacts"][0]["deliveries"][0]["status"]
        assert delivery_status in {"failed", "skipped"}
        assert row["steps"][-1]["status"] == delivery_status
