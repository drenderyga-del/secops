from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.execution_contract import ExecutionStatus, LegacySource, StepStatus
from app.execution_service import ExecutionService
from app.alerting.ptai_scan_status import _observe_shared_execution
from app.config_scan_service import ConfigScanService
from app.models.config_scan import ConfigScanRun
from app.models.execution import Execution, ExecutionEvent, ExecutionStep
from app.models.artifact import Artifact, ArtifactProvenance, Delivery
from app.models.product import Product
from app.models.repository import Repository
from app.models.pipeline import ActivePipeline, PipelineJob, PipelineStatus
from app.database import DatabaseService


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


def _session():
    engine = create_engine("sqlite:///:memory:")
    Execution.__table__.create(engine)
    ExecutionStep.__table__.create(engine)
    ExecutionEvent.__table__.create(engine)
    Artifact.__table__.create(engine)
    Delivery.__table__.create(engine)
    ArtifactProvenance.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_execution_service_replays_same_source_correlation_and_step_idempotently():
    db = _session()
    service = ExecutionService(db)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    execution = service.observe_execution(
        source="gitlab_pipeline",
        correlation_key="gitlab:42:100",
        repository_id=7,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=now,
    )
    replay = service.observe_execution(
        source="gitlab_pipeline",
        correlation_key="gitlab:42:100",
        repository_id=7,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=now,
    )
    step = service.observe_step(
        execution,
        source="gitlab_job",
        external_id="job:500",
        name="sast",
        status=StepStatus.RUNNING,
        observed_at=now,
    )
    replayed_step = service.observe_step(
        replay,
        source="gitlab_job",
        external_id="job:500",
        name="sast",
        status=StepStatus.SUCCEEDED,
        observed_at=now,
    )

    assert execution.id == replay.id
    assert step.id == replayed_step.id
    assert db.query(Execution).count() == 1
    assert db.query(ExecutionStep).count() == 1
    assert replayed_step.status == StepStatus.SUCCEEDED.value


def test_execution_service_rejects_regression_from_terminal_status():
    db = _session()
    service = ExecutionService(db)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    service.observe_execution(
        source="config_scan",
        correlation_key="config:33",
        repository_id=None,
        product_id=3,
        status=ExecutionStatus.SUCCEEDED,
        observed_at=now,
    )

    with pytest.raises(ValueError, match="Invalid execution transition"):
        service.observe_execution(
            source="config_scan",
            correlation_key="config:33",
            repository_id=None,
            product_id=3,
            status=ExecutionStatus.RUNNING,
            observed_at=now,
        )


def test_terminal_execution_replay_preserves_original_finished_at():
    db = _session()
    service = ExecutionService(db)
    first_seen = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    replayed_at = datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc)
    service.observe_execution(
        source="config_scan",
        correlation_key="config:34",
        repository_id=None,
        product_id=3,
        status=ExecutionStatus.SUCCEEDED,
        observed_at=first_seen,
    )

    replay = service.observe_execution(
        source="config_scan",
        correlation_key="config:34",
        repository_id=None,
        product_id=3,
        status=ExecutionStatus.SUCCEEDED,
        observed_at=replayed_at,
    )

    assert replay.finished_at == first_seen.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("source", "raw_status", "expected"),
    [
        (LegacySource.GITLAB_PIPELINE, "polling", ExecutionStatus.RUNNING),
        (LegacySource.IAC_RUN, "partial", ExecutionStatus.PARTIAL),
        (LegacySource.CONFIG_SCAN, "succeeded", ExecutionStatus.SUCCEEDED),
        (LegacySource.PTAI_ASOC, "ERROR", ExecutionStatus.FAILED),
    ],
)
def test_execution_service_maps_every_scanner_family_to_one_execution_lifecycle(source, raw_status, expected):
    db = _session()
    service = ExecutionService(db)

    execution = service.observe_legacy_execution(
        source=source,
        raw_status=raw_status,
        correlation_key=f"{source.value}:1",
        repository_id=7,
        product_id=3,
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert execution.status == expected.value


def test_config_scan_writer_updates_the_shared_execution_lifecycle():
    engine = create_engine("sqlite:///:memory:")
    Product.__table__.create(engine)
    ConfigScanRun.__table__.create(engine)
    Execution.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.example")
    db.add(product)
    db.commit()
    service = ConfigScanService(db)

    run = service.create_run(product.id)
    service.mark_run_started(run.id)
    service.complete_run(run.id, {"projects_total": 0, "projects_scanned": 0}, 0)

    execution = db.query(Execution).one()
    assert execution.correlation_key == f"config_scan:{run.id}"
    assert execution.status == ExecutionStatus.SUCCEEDED.value


def test_legacy_job_observation_updates_one_step_and_rejects_terminal_regression():
    db = _session()
    service = ExecutionService(db)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    execution = service.observe_execution(
        source="gitlab_pipeline",
        correlation_key="gitlab:42:100",
        repository_id=7,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=now,
    )

    first = service.observe_legacy_step(
        execution,
        source=LegacySource.GITLAB_JOB,
        raw_status="running",
        external_id="job:500",
        name="sast",
        observed_at=now,
    )
    completed = service.observe_legacy_step(
        execution,
        source=LegacySource.GITLAB_JOB,
        raw_status="success",
        external_id="job:500",
        name="sast",
        observed_at=now,
    )

    assert first.id == completed.id
    assert completed.status == StepStatus.SUCCEEDED.value
    with pytest.raises(ValueError, match="Invalid step transition"):
        service.observe_legacy_step(
            execution,
            source=LegacySource.GITLAB_JOB,
            raw_status="running",
            external_id="job:500",
            name="sast",
            observed_at=now,
        )


def test_step_observation_rejects_nonterminal_regression():
    db = _session()
    service = ExecutionService(db)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    execution = service.observe_execution(
        source="gitlab_pipeline",
        correlation_key="gitlab:42:101",
        repository_id=7,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=now,
    )
    service.observe_step(
        execution,
        source="gitlab_job",
        external_id="job:501",
        name="sast",
        status=StepStatus.RUNNING,
        observed_at=now,
    )

    with pytest.raises(ValueError, match="Invalid step transition"):
        service.observe_step(
            execution,
            source="gitlab_job",
            external_id="job:501",
            name="sast",
            status=StepStatus.QUEUED,
            observed_at=now,
        )


def test_repository_timeline_returns_newest_executions_with_ordered_steps():
    db = _session()
    service = ExecutionService(db)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    execution = service.observe_execution(
        source="gitlab_pipeline",
        correlation_key="gitlab:42:100",
        repository_id=7,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=now,
    )
    service.observe_step(
        execution,
        source="gitlab_job",
        external_id="job:500",
        name="sast",
        status=StepStatus.RUNNING,
        observed_at=now,
    )
    db.commit()

    timeline = service.repository_timeline(7)

    assert timeline[0]["correlation_key"] == "gitlab:42:100"
    assert timeline[0]["steps"][0]["name"] == "sast"


def test_latest_evidence_uses_domain_time_and_normalizes_timezone_offsets_before_comparison():
    execution = SimpleNamespace(
        finished_at=datetime.fromisoformat("2026-07-21T10:00:00+10:00"),
        started_at=None,
        requested_at=None,
        created_at=datetime.fromisoformat("2026-07-21T02:00:00+00:00"),
        events=[],
        steps=[],
        artifacts=[],
    )

    assert ExecutionService._latest_evidence_at(execution) == datetime.fromisoformat("2026-07-21T00:00:00+00:00")


def test_ptai_poller_records_terminal_status_in_shared_execution():
    db = _session()
    finished_at = datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc)
    scan = SimpleNamespace(
        asoc_status="SUCCESS",
        gitlab_job_id=501,
        repository_id=7,
        product_id=3,
        finished_at=finished_at,
    )

    _observe_shared_execution(db, scan, finished_at)

    execution = db.query(Execution).one()
    assert execution.correlation_key == "ptai_asoc:501"
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert execution.finished_at == finished_at.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("job_status", "expected_pipeline_status", "expected_execution_status"),
    [
        ("success", PipelineStatus.COMPLETED, ExecutionStatus.SUCCEEDED),
        ("failed", PipelineStatus.FAILED, ExecutionStatus.FAILED),
        ("canceled", PipelineStatus.FAILED, ExecutionStatus.FAILED),
        ("cancelled", PipelineStatus.FAILED, ExecutionStatus.FAILED),
    ],
)
def test_terminal_gitlab_pipeline_uses_tracked_job_outcome(
    job_status,
    expected_pipeline_status,
    expected_execution_status,
):
    engine = create_engine("sqlite:///:memory:")
    for table in (
        Product.__table__,
        Repository.__table__,
        ActivePipeline.__table__,
        PipelineJob.__table__,
        Execution.__table__,
        ExecutionStep.__table__,
    ):
        table.create(engine)
    db = sessionmaker(bind=engine)()
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.example")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/api")
    db.add(repository)
    db.flush()
    pipeline = ActivePipeline(
        project_id=42,
        pipeline_id=100,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.POLLING,
    )
    db.add(pipeline)
    db.flush()
    db.add(
        PipelineJob(
            active_pipeline_id=pipeline.id,
            gitlab_job_id=501,
            name="sast",
            status=job_status,
        )
    )
    ExecutionService(db).observe_execution(
        source=LegacySource.GITLAB_PIPELINE.value,
        correlation_key="gitlab_pipeline:42:100",
        repository_id=repository.id,
        product_id=product.id,
        status=ExecutionStatus.RUNNING,
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    db.commit()

    DatabaseService(db).update_pipeline_poll_state(
        pipeline,
        completed=True,
        observed_job_statuses=[job_status],
    )

    execution = db.query(Execution).one()
    db.refresh(pipeline)
    assert pipeline.status == expected_pipeline_status
    assert execution.status == expected_execution_status.value
    assert execution.finished_at is not None
    if expected_pipeline_status is PipelineStatus.FAILED:
        assert pipeline.error_message == "Tracked job failed or was cancelled"
    else:
        assert pipeline.error_message is None


def test_terminal_pipeline_ignores_superseded_and_untracked_failed_jobs():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        Product.__table__,
        Repository.__table__,
        ActivePipeline.__table__,
        PipelineJob.__table__,
        Execution.__table__,
        ExecutionStep.__table__,
    ):
        table.create(engine)
    db = sessionmaker(bind=engine)()
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.example")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/api")
    db.add(repository)
    db.flush()
    pipeline = ActivePipeline(
        project_id=42,
        pipeline_id=102,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.POLLING,
        tracked_jobs=["sast"],
    )
    db.add(pipeline)
    db.flush()
    db.add_all(
        [
            PipelineJob(
                active_pipeline_id=pipeline.id,
                gitlab_job_id=500,
                name="sast",
                status="failed",
            ),
            PipelineJob(
                active_pipeline_id=pipeline.id,
                gitlab_job_id=501,
                name="legacy_scan",
                status="failed",
            ),
            PipelineJob(
                active_pipeline_id=pipeline.id,
                gitlab_job_id=502,
                name="sast",
                status="success",
            ),
        ]
    )
    ExecutionService(db).observe_execution(
        source=LegacySource.GITLAB_PIPELINE.value,
        correlation_key="gitlab_pipeline:42:102",
        repository_id=repository.id,
        product_id=product.id,
        status=ExecutionStatus.RUNNING,
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    db.commit()

    DatabaseService(db).update_pipeline_poll_state(
        pipeline,
        completed=True,
        observed_job_statuses=["success"],
    )

    db.refresh(pipeline)
    execution = db.query(Execution).one()
    assert pipeline.status == PipelineStatus.COMPLETED
    assert pipeline.error_message is None
    assert execution.status == ExecutionStatus.SUCCEEDED.value


def test_terminal_pipeline_poll_error_is_a_failed_outcome():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        Product.__table__,
        Repository.__table__,
        ActivePipeline.__table__,
        PipelineJob.__table__,
        Execution.__table__,
        ExecutionStep.__table__,
    ):
        table.create(engine)
    db = sessionmaker(bind=engine)()
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.example")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/api")
    db.add(repository)
    db.flush()
    pipeline = ActivePipeline(
        project_id=42,
        pipeline_id=101,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.POLLING,
    )
    db.add(pipeline)
    ExecutionService(db).observe_execution(
        source=LegacySource.GITLAB_PIPELINE.value,
        correlation_key="gitlab_pipeline:42:101",
        repository_id=repository.id,
        product_id=product.id,
        status=ExecutionStatus.RUNNING,
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    db.commit()

    DatabaseService(db).update_pipeline_poll_state(
        pipeline,
        completed=True,
        error="Pipeline not found (treated as complete)",
    )

    db.refresh(pipeline)
    execution = db.query(Execution).one()
    assert pipeline.status == PipelineStatus.FAILED
    assert pipeline.error_message == "Pipeline not found (treated as complete)"
    assert execution.status == ExecutionStatus.FAILED.value


def test_concurrent_execution_insert_replays_existing_row(monkeypatch):
    db = _session()
    existing = ExecutionService(db).observe_execution(
        source="config_scan",
        correlation_key="config:concurrent",
        repository_id=None,
        product_id=3,
        status=ExecutionStatus.QUEUED,
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    db.commit()
    real_query = db.query
    miss_once = {"value": True}

    class _Miss:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            miss_once["value"] = False
            return None

    def query_with_stale_first_read(*entities, **kwargs):
        if miss_once["value"] and entities == (Execution,):
            return _Miss()
        return real_query(*entities, **kwargs)

    monkeypatch.setattr(db, "query", query_with_stale_first_read)

    replay = ExecutionService(db).observe_execution(
        source="config_scan",
        correlation_key="config:concurrent",
        repository_id=None,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=datetime(2026, 7, 20, 0, 1, tzinfo=timezone.utc),
    )

    assert replay.id == existing.id
    assert replay.status == ExecutionStatus.RUNNING.value


def test_concurrent_step_insert_replays_existing_row(monkeypatch):
    db = _session()
    service = ExecutionService(db)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    execution = service.observe_execution(
        source="gitlab_pipeline",
        correlation_key="gitlab:concurrent",
        repository_id=7,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=now,
    )
    existing = service.observe_step(
        execution,
        source="gitlab_job",
        external_id="job:concurrent",
        name="sast",
        status=StepStatus.QUEUED,
        observed_at=now,
    )
    db.commit()
    real_query = db.query
    miss_once = {"value": True}

    class _Miss:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            miss_once["value"] = False
            return None

    def query_with_stale_first_read(*entities, **kwargs):
        if miss_once["value"] and entities == (ExecutionStep,):
            return _Miss()
        return real_query(*entities, **kwargs)

    monkeypatch.setattr(db, "query", query_with_stale_first_read)

    replay = service.observe_step(
        execution,
        source="gitlab_job",
        external_id="job:concurrent",
        name="sast",
        status=StepStatus.RUNNING,
        observed_at=now,
    )

    assert replay.id == existing.id
    assert replay.status == StepStatus.RUNNING.value
