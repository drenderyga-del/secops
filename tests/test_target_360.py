from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.artifact_delivery_service import ArtifactDeliveryService, DeliveryStatus
from app.config_scan_service import ConfigScanService
from app.execution_contract import ExecutionStatus, StepStatus
from app.execution_event_service import ExecutionEventService
from app.execution_service import ExecutionService
from app.models import (
    ActivePipeline,
    Artifact,
    ArtifactProvenance,
    ConfigScanFinding,
    ConfigScanRun,
    Delivery,
    Execution,
    ExecutionEvent,
    ExecutionStep,
    JobReport,
    JobReportDetail,
    JobStatus,
    PipelineStatus,
    Product,
    Repository,
    RepositoryControlBinding,
    SecurityControl,
)
from app.schemas import ExecutionEventIngestRequest
from app.target_360_service import Target360Service
from app.target_navigation import product_target_url, repository_target_url


def _session():
    engine = create_engine("sqlite://")
    for model in (
        Product,
        Repository,
        ActivePipeline,
        JobReport,
        JobReportDetail,
        SecurityControl,
        RepositoryControlBinding,
        ConfigScanRun,
        ConfigScanFinding,
        Execution,
        ExecutionStep,
        Artifact,
        Delivery,
        ArtifactProvenance,
        ExecutionEvent,
    ):
        model.__table__.create(engine)
    return sessionmaker(bind=engine)()


def _target(db):
    product = Product(name="ExampleApp", path="platform/example_app", gitlab_base_url="https://gitlab.example")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/example_app/api")
    db.add(repository)
    db.flush()
    return product, repository


def test_target_360_combines_controls_runs_events_artifacts_and_delivery():
    db = _session()
    product, repository = _target(db)
    sast = SecurityControl(control_key="sast", name="SAST", category="code")
    iac = SecurityControl(control_key="iac", name="IaC", category="infrastructure")
    db.add_all([sast, iac])
    db.flush()
    db.add_all(
        [
            RepositoryControlBinding(
                repository_id=repository.id,
                security_control_id=sast.id,
                status="configured",
                source="gitlab",
                last_verified_at=datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc),
            ),
            RepositoryControlBinding(
                repository_id=repository.id,
                security_control_id=iac.id,
                status="missing",
                source="config_scan",
                reason="No include found",
            ),
        ]
    )
    now = datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc)
    execution = ExecutionService(db).observe_execution(
        source="iac_repository",
        correlation_key="iac:42:100",
        repository_id=repository.id,
        product_id=product.id,
        status=ExecutionStatus.RUNNING,
        observed_at=now - timedelta(minutes=8),
    )
    step = ExecutionService(db).observe_step(
        execution,
        source="checkov",
        external_id="scan:100",
        name="Checkov scan",
        status=StepStatus.SUCCEEDED,
        observed_at=now - timedelta(minutes=6),
    )
    contract = ArtifactDeliveryService(db)
    artifact = contract.record_artifact(
        execution=execution,
        source="checkov",
        external_id="sarif:100",
        kind="sarif",
        uri="reports/100.sarif",
        digest_sha256="b" * 64,
        size_bytes=512,
        schema_version="2.1.0",
        observed_at=now - timedelta(minutes=5),
    )
    contract.record_provenance(
        artifact=artifact,
        execution=execution,
        execution_step=step,
        source_revision="deadbeef",
        source_ref="main",
        observed_at=now - timedelta(minutes=5),
    )
    contract.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc",
        status=DeliveryStatus.CONFIRMED,
        external_id="report:100",
        observed_at=now - timedelta(minutes=4),
    )
    ExecutionEventService(db).ingest(
        ExecutionEventIngestRequest(
            schema_version="1.0",
            event_id="evt-100-success",
            event_type="execution.transition",
            source="iac_repository",
            correlation_key="iac:42:100",
            observed_at=now - timedelta(minutes=3),
            repository_id=repository.id,
            product_id=product.id,
            reason="artifact delivered",
            data={"status": "succeeded", "scanner_version": "3.2.1"},
        )
    )
    db.commit()

    snapshot = Target360Service(db).snapshot(repository.id, now=now)

    assert snapshot["coverage"] == {"expected": 2, "configured": 1, "missing": 1, "excluded": 0}
    assert snapshot["freshness"]["state"] == "fresh"
    assert snapshot["freshness"]["observed_at"].startswith("2026-07-21T01:57:00")
    assert snapshot["summary"]["executions"] == 1
    assert snapshot["summary"]["artifacts"] == 1
    assert snapshot["summary"]["confirmed_deliveries"] == 1
    assert snapshot["versions"] == ["Checkov · 3.2.1", "SARIF · 2.1.0"]
    assert snapshot["executions"][0]["events"][0]["reason"] == "artifact delivered"
    assert next(item for item in snapshot["controls"] if item["key"] == "iac")["reason"] == "No include found"


def test_target_360_separates_pipeline_outcome_from_unresolved_job_failure_and_links_gitlab():
    db = _session()
    product, repository = _target(db)
    old_pipeline = ActivePipeline(
        project_id=501,
        pipeline_id=9000,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.FAILED,
        web_url="https://gitlab.example/platform/example_app/api/-/pipelines/9000",
    )
    latest_pipeline = ActivePipeline(
        project_id=501,
        pipeline_id=9001,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.COMPLETED,
        web_url="https://gitlab.example/platform/example_app/api/-/pipelines/9001",
    )
    db.add_all([old_pipeline, latest_pipeline])
    db.flush()
    observed_at = datetime(2026, 9, 24, 18, 50, tzinfo=timezone.utc)
    ExecutionService(db).observe_execution(
        source="gitlab_pipeline",
        correlation_key="gitlab_pipeline:501:9001",
        repository_id=repository.id,
        product_id=product.id,
        status=ExecutionStatus.SUCCEEDED,
        observed_at=observed_at,
    )
    failed_job = JobReport(
        repository_id=repository.id,
        job_name="semgrep_scan",
        job_status=JobStatus.FAILED,
        pipeline_url=old_pipeline.web_url,
        ref="feature/old-scan",
        finished_at=observed_at - timedelta(days=7),
    )
    db.add(failed_job)
    db.flush()
    db.add(
        JobReportDetail(
            job_report_id=failed_job.id,
            raw_payload={"job_url": "https://gitlab.example/platform/example_app/api/-/jobs/88"},
        )
    )
    db.commit()

    snapshot = Target360Service(db).snapshot(repository.id, now=observed_at)

    assert snapshot["gitlab_repository_url"] == "https://gitlab.example/platform/example_app/api"
    assert snapshot["executions"][0]["status"] == "succeeded"
    assert snapshot["executions"][0]["gitlab_url"] == latest_pipeline.web_url
    assert snapshot["job_health"]["broken_keys"] == 1
    failure = snapshot["job_health"]["failures"][0]
    assert failure["name"] == "semgrep_scan"
    assert failure["ref"] == "feature/old-scan"
    assert failure["finished_at"].startswith("2026-09-17T18:50:00")
    assert failure["job_url"] == "https://gitlab.example/platform/example_app/api/-/jobs/88"
    assert failure["pipeline_url"] == old_pipeline.web_url
    assert snapshot["job_health"]["failures_href"] == (
        f"/repos/{repository.id}/jobs?only_failed=true&status=failed&timeframe=all"
    )


def test_config_scan_projects_repository_observation_into_shared_execution():
    db = _session()
    product, repository = _target(db)
    run = ConfigScanRun(product_id=product.id, status="running", trigger="manual")
    db.add(run)
    db.commit()

    ConfigScanService(db).save_findings(
        run.id,
        product.id,
        [{"project_path": repository.path, "project_url": "https://gitlab.example/platform/example_app/api", "findings": []}],
    )

    execution = db.query(Execution).filter(Execution.repository_id == repository.id).one()
    assert execution.source == "config_scan"
    assert execution.status == "succeeded"
    assert execution.steps[0].name == "Configuration evaluation"


def test_source_filter_queries_matching_history_beyond_unfiltered_limit():
    db = _session()
    product, repository = _target(db)
    service = ExecutionService(db)
    observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    service.observe_execution(
        source="config_scan",
        correlation_key="config:old",
        repository_id=repository.id,
        product_id=product.id,
        status=ExecutionStatus.SUCCEEDED,
        observed_at=observed_at,
    )
    for index in range(3):
        service.observe_execution(
            source="gitlab_pipeline",
            correlation_key=f"pipeline:{index}",
            repository_id=repository.id,
            product_id=product.id,
            status=ExecutionStatus.SUCCEEDED,
            observed_at=observed_at + timedelta(minutes=index + 1),
        )
    db.commit()

    snapshot = Target360Service(db).snapshot(repository.id, source="config_scan", limit=2)

    assert [execution["correlation_key"] for execution in snapshot["executions"]] == ["config:old"]
    assert snapshot["summary"]["executions"] == 4
    assert snapshot["source_options"][0]["count"] == 4


def test_target_360_template_exposes_end_to_end_operator_context():
    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    env.globals.update(
        product_target_url=product_target_url,
        repository_target_url=repository_target_url,
    )
    html = env.get_template("repository_360.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/targets/repositories/7")),
        ui_auth_enabled=True,
        repository=SimpleNamespace(id=7, path="platform/example_app/api"),
        product=SimpleNamespace(name="ExampleApp", id=3),
        gitlab_repository_url="https://gitlab.example/platform/example_app/api",
        job_health={
            "healthy_keys": 1,
            "broken_keys": 1,
            "total_keys": 2,
            "failures": [{"name": "semgrep_scan", "ref": "feature/old-scan", "finished_at": "2026-07-20T01:57:00+00:00", "job_url": "https://gitlab.example/platform/example_app/api/-/jobs/88", "pipeline_url": "https://gitlab.example/platform/example_app/api/-/pipelines/9000"}],
            "failures_href": "/repos/7/jobs?only_failed=true&status=failed&timeframe=all",
        },
        coverage={"expected": 2, "configured": 1, "missing": 1, "excluded": 0},
        freshness={"state": "fresh", "label": "8 min ago", "observed_at": "2026-07-21T01:52:00+00:00"},
        summary={"executions": 1, "artifacts": 1, "confirmed_deliveries": 1, "failed_executions": 0},
        versions=["Checkov · 3.2.1", "SARIF · 2.1.0"],
        controls=[
            {"key": "sast", "name": "SAST", "category": "code", "scope": "*", "status": "configured", "source": "gitlab", "reason": None, "evidence": "include", "last_verified_at": "2026-07-21T01:00:00+00:00"}
        ],
        source_options=[{"value": "all", "label": "All sources", "count": 1}, {"value": "iac_repository", "label": "IaC", "count": 1}],
        selected_source="all",
        executions=[
            {
                "id": 10,
                "source": "iac_repository",
                "source_label": "IaC",
                "correlation_key": "iac:42:100",
                "gitlab_url": "https://gitlab.example/platform/example_app/api/-/pipelines/9001",
                "status": "succeeded",
                "failure_category": None,
                "failure_reason": None,
                "requested_at": "2026-07-21T01:52:00+00:00",
                "started_at": "2026-07-21T01:52:00+00:00",
                "finished_at": "2026-07-21T01:57:00+00:00",
                "steps": [{"name": "Checkov scan", "status": "succeeded", "source": "checkov", "started_at": None, "finished_at": "2026-07-21T01:54:00+00:00", "failure_reason": None}],
                "artifacts": [{"kind": "sarif", "schema_version": "2.1.0", "digest_sha256": "b" * 64, "uri": "reports/100.sarif", "provenance": {"source_ref": "main", "source_revision": "deadbeef"}, "deliveries": [{"destination": "asoc", "status": "confirmed", "attempt_key": "initial", "confirmed_at": "2026-07-21T01:56:00+00:00", "failure_reason": None}]}],
                "events": [{"event_type": "execution.transition", "source": "iac_repository", "reason": "artifact delivered", "observed_at": "2026-07-21T01:57:00+00:00", "received_at": "2026-07-21T01:57:01+00:00", "data": {"status": "succeeded"}}],
            }
        ],
    )

    for label in ("Target 360", "Control coverage", "Freshness", "Observed versions", "Execution timeline", "Artifacts & delivery", "Event evidence", "ASOC"):
        assert label in html
    assert 'href="/repos/7/jobs"' in html
    assert 'href="/repos/7/jobs?only_failed=true&status=failed&timeframe=all"' in html
    assert 'href="/targets/products/3"' in html
    assert 'href="/targets/repositories/7?source=all"' in html
    assert 'href="https://gitlab.example/platform/example_app/api"' in html
    assert 'href="https://gitlab.example/platform/example_app/api/-/pipelines/9001"' in html
    assert 'href="https://gitlab.example/platform/example_app/api/-/jobs/88"' in html
    assert "unresolved failed job key" in html


@pytest.mark.parametrize('origin', ['report', 'detail', 'pipeline'])
@pytest.mark.parametrize('suffix', ['?private_token=' + 'x' * 32 + '#access_token=hidden',
                                     ';private_token=' + 'glpat-' + 'a' * 20,
                                     '%3Fprivate_token%3D' + 'glpat-' + 'a' * 20,
                                     '/prefix-glpat-' + 'a' * 20,
                                     '/glpat-' + 'a' * 20 + '.json',
                                     ';download=prefix-glpat-' + 'a' * 20])
def test_persisted_job_evidence_never_projects_credentials(origin, suffix):
    import json
    db = _session(); product, repository = _target(db)
    clean = 'https://gitlab.example/platform/example_app/api/-/pipelines/15'
    unsafe = clean + suffix
    payload = {'job_url': 'https://gitlab.example/platform/example_app/api/-/jobs/8#token=hidden'}
    if origin == 'detail':
        payload['pipeline_url'] = unsafe
    elif origin == 'pipeline':
        db.add(ActivePipeline(project_id=501, pipeline_id=15, repository_id=repository.id,
                              product_id=product.id, status=PipelineStatus.FAILED, web_url=unsafe))
        payload.update(project_id=501, pipeline_id=15)
    report = JobReport(repository_id=repository.id, job_name='scanner', job_status=JobStatus.FAILED,
                       ref='main', finished_at=datetime.now(timezone.utc),
                       pipeline_url=unsafe if origin == 'report' else None)
    db.add(report); db.flush()
    db.add(JobReportDetail(job_report_id=report.id, raw_payload=payload)); db.commit()
    snapshot = Target360Service(db).snapshot(repository.id)
    failure = snapshot['job_health']['failures'][0]
    assert failure['pipeline_url'] == (clean if suffix.startswith('?') else None)
    assert failure['job_url'] == 'https://gitlab.example/platform/example_app/api/-/jobs/8'
    serialized = json.dumps(snapshot['job_health'])
    assert 'private_token' not in serialized and 'access_token' not in serialized
    assert 'x' * 32 not in serialized and 'glpat-' not in serialized
    env = Environment(loader=FileSystemLoader('app/templates'))
    env.filters['localtime'] = lambda value, _fmt=None: str(value or '')
    env.globals.update(product_target_url=product_target_url, repository_target_url=repository_target_url)
    html = env.get_template('repository_360.html').render(
        **snapshot, request=SimpleNamespace(url=SimpleNamespace(path='/targets/repositories/1')),
        ui_auth_enabled=True)
    assert 'private_token' not in html and 'glpat-' not in html and 'x' * 32 not in html


def test_repeated_failures_load_only_latest_detail_per_job_ref_key():
    from sqlalchemy import event
    db = _session(); product, repository = _target(db)
    repository_id, path = repository.id, repository.path
    now = datetime.now(timezone.utc)
    for index in range(250):
        report = JobReport(repository_id=repository_id, job_name='repeated', job_status=JobStatus.FAILED,
                           ref=None, finished_at=now + timedelta(seconds=index))
        db.add(report); db.flush()
        db.add(JobReportDetail(job_report_id=report.id, raw_payload={'job_url': f'https://gitlab.example/jobs/{index}'}))
    db.commit(); db.expunge_all()
    loaded = []
    def observed(report, context):
        loaded.append(report.id)
    event.listen(JobReport, 'load', observed)
    try:
        health = Target360Service(db)._job_health(repository_id, path, {}, {'job_health_broken': 1})
    finally:
        event.remove(JobReport, 'load', observed)
    assert len(loaded) == 1
    assert len(health['failures']) == 1
    assert health['failures'][0]['job_url'] == 'https://gitlab.example/jobs/249'
    assert not health['failures_truncated']


def test_failure_detail_limit_preserves_total_and_marks_truncation():
    db = _session(); product, repository = _target(db)
    now = datetime.now(timezone.utc)
    for index in range(105):
        db.add(JobReport(repository_id=repository.id, job_name='scanner', ref=f'branch-{index}',
                         job_status=JobStatus.FAILED, finished_at=now + timedelta(seconds=index)))
    db.commit()
    health = Target360Service(db)._job_health(repository.id, repository.path, {}, {'job_health_broken': 105})
    assert health['broken_keys'] == 105
    assert len(health['failures']) == health['failures_limit'] == 100
    assert health['failures_truncated'] is True
    assert health['failures'][0]['ref'] == 'branch-104'
    assert 'timeframe=all' in health['failures_href']


def test_target_360_projects_only_needed_pipeline_links_without_loading_history():
    from sqlalchemy import event
    db = _session(); product, repository = _target(db)
    repository_id = repository.id
    now = datetime.now(timezone.utc)
    for index in range(1, 501):
        db.add(ActivePipeline(project_id=501, pipeline_id=index, repository_id=repository_id,
                              product_id=product.id, status=PipelineStatus.FAILED,
                              web_url=f'https://gitlab.example/platform/example_app/api/-/pipelines/{index}'))
    execution = ExecutionService(db).observe_execution(
        source='gitlab_pipeline', correlation_key='gitlab_pipeline:501:500',
        repository_id=repository_id, product_id=product.id,
        status=ExecutionStatus.FAILED, observed_at=now)
    report = JobReport(repository_id=repository_id, job_name='scanner', ref='main',
                       job_status=JobStatus.FAILED, finished_at=now)
    db.add(report); db.flush()
    db.add(JobReportDetail(job_report_id=report.id, raw_payload={'project_id': 501, 'pipeline_id': 1}))
    db.commit(); db.expunge_all()
    loaded = []
    def observed(row, context):
        loaded.append(row.id)
    event.listen(ActivePipeline, 'load', observed)
    try:
        snapshot = Target360Service(db).snapshot(repository_id, limit=1)
    finally:
        event.remove(ActivePipeline, 'load', observed)
    assert loaded == []
    assert snapshot['executions'][0]['gitlab_url'].endswith('/pipelines/500')
    assert snapshot['job_health']['failures'][0]['pipeline_url'].endswith('/pipelines/1')
