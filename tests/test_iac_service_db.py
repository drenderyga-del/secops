"""IacService behavior against a real (in-memory sqlite) database:
duplicate-run guards and the latest-per-repo state view."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Product, Repository
from app.models.iac import IacScanFinding, IacScanRepository, IacScanRun
from app.models.execution import Execution, ExecutionStep
from app.models.artifact import Artifact, ArtifactProvenance, Delivery
from app.iac_service import IacService


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Product.__table__,
            Repository.__table__,
            IacScanRun.__table__,
            IacScanRepository.__table__,
            IacScanFinding.__table__,
            Execution.__table__,
            ExecutionStep.__table__,
            Artifact.__table__,
            Delivery.__table__,
            ArtifactProvenance.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def make_run(db, status="succeeded", scope="sandbox/portal", **overrides) -> IacScanRun:
    values = dict(
        status=status,
        trigger="manual",
        target_url=f"https://gitlab.enterprise.example.com/{scope}",
        gitlab_base_url="https://gitlab.enterprise.example.com",
        scope_path=scope,
    )
    values.update(overrides)
    run = IacScanRun(**values)
    db.add(run)
    db.commit()
    return run


def make_repo_row(db, run, project_path="sandbox/portal/app", **overrides) -> IacScanRepository:
    values = dict(
        run_id=run.id,
        gitlab_project_id=101,
        project_path=project_path,
        project_url=f"https://gitlab.enterprise.example.com/{project_path}",
        finder_status="succeeded",
        scan_status="succeeded",
        asoc_upload_status="uploaded",
        has_iac=True,
    )
    values.update(overrides)
    row = IacScanRepository(**values)
    db.add(row)
    db.commit()
    return row


def test_create_run_returns_existing_queued_duplicate(db):
    service = IacService(db)
    first = service.create_run(
        target_url="https://gitlab.enterprise.example.com/sandbox/portal",
        gitlab_base_url="https://gitlab.enterprise.example.com",
        scope_path="sandbox/portal",
    )
    second = service.create_run(
        target_url="https://gitlab.enterprise.example.com/sandbox/portal",
        gitlab_base_url="https://gitlab.enterprise.example.com",
        scope_path="sandbox/portal",
    )
    assert second.id == first.id
    assert db.query(IacScanRun).count() == 1
    execution = db.query(Execution).one()
    assert execution.source == "iac_run"
    assert execution.correlation_key == f"iac_run:{first.id}"
    assert execution.status == "queued"


def test_iac_repository_execution_requires_artifact_and_confirmed_delivery(db):
    service = IacService(db)
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.enterprise.example.com")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/api")
    db.add(repository)
    run = make_run(db, status="running")
    row = make_repo_row(
        db,
        run,
        product_id=product.id,
        repository_id=repository.id,
        finder_status="succeeded",
        scan_status="succeeded",
        asoc_upload_status="pending",
        sarif_path="/reports/run-api.sarif",
    )

    execution = service.sync_repository_execution(
        row,
        artifact_digest_sha256="e" * 64,
        artifact_size_bytes=128,
    )
    assert execution.status == "delivering"
    assert db.query(Artifact).count() == 1
    assert db.query(ArtifactProvenance).count() == 1
    assert db.query(Delivery).one().status == "requested"

    row.asoc_upload_status = "uploaded"
    row.asoc_repository_id = "asoc-42"
    execution = service.sync_repository_execution(row)

    assert execution.status == "succeeded"
    assert db.query(Delivery).one().status == "confirmed"


def test_iac_repository_no_iac_is_success_without_artifact(db):
    service = IacService(db)
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.enterprise.example.com")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/empty")
    db.add(repository)
    run = make_run(db, status="running", scope="platform")
    row = make_repo_row(
        db,
        run,
        product_id=product.id,
        repository_id=repository.id,
        finder_status="succeeded",
        scan_status="no_iac",
        asoc_upload_status="skipped",
        has_iac=False,
    )

    execution = service.sync_repository_execution(row)

    assert execution.status == "succeeded"
    assert db.query(Artifact).count() == 0


def test_iac_manual_reupload_records_new_attempt_without_rewriting_terminal_execution(db):
    service = IacService(db)
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.enterprise.example.com")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/retry")
    db.add(repository)
    run = make_run(db, status="running", scope="platform")
    row = make_repo_row(
        db,
        run,
        product_id=product.id,
        repository_id=repository.id,
        finder_status="succeeded",
        scan_status="succeeded",
        asoc_upload_status="skipped",
        sarif_path="/reports/retry.sarif",
    )
    execution = service.sync_repository_execution(
        row,
        artifact_digest_sha256="1" * 64,
        artifact_size_bytes=128,
    )
    assert execution.status == "failed"

    row.asoc_upload_status = "uploaded"
    row.asoc_repository_id = "asoc-retry"
    replay = service.sync_repository_execution(row, delivery_attempt_key="manual:1")

    assert replay.status == "failed"
    assert [item.status for item in db.query(Delivery).order_by(Delivery.id).all()] == ["skipped", "confirmed"]


def test_iac_manual_reupload_backfills_artifact_from_existing_sarif(db, tmp_path):
    service = IacService(db)
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.enterprise.example.com")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/historical")
    db.add(repository)
    run = make_run(db, status="running", scope="platform")
    sarif_path = tmp_path / "historical.sarif"
    sarif_path.write_text('{"version":"2.1.0","runs":[]}', encoding="utf-8")
    row = make_repo_row(
        db,
        run,
        product_id=product.id,
        repository_id=repository.id,
        finder_status="succeeded",
        scan_status="succeeded",
        asoc_upload_status="uploaded",
        asoc_repository_id="asoc-historical",
        sarif_path=str(sarif_path),
    )

    execution = service.sync_repository_execution(row, delivery_attempt_key="manual:historical")

    assert execution.status == "succeeded"
    assert db.query(Artifact).one().size_bytes == sarif_path.stat().st_size
    assert db.query(Delivery).one().attempt_key == "manual:historical"


def test_iac_manual_reupload_rejects_invalid_existing_sarif(db, tmp_path):
    service = IacService(db)
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.enterprise.example.com")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="platform/invalid")
    db.add(repository)
    run = make_run(db, status="running", scope="platform")
    sarif_path = tmp_path / "invalid.sarif"
    sarif_path.write_text("{}", encoding="utf-8")
    row = make_repo_row(
        db,
        run,
        product_id=product.id,
        repository_id=repository.id,
        finder_status="succeeded",
        scan_status="succeeded",
        asoc_upload_status="uploaded",
        sarif_path=str(sarif_path),
    )

    with pytest.raises(ValueError, match="Invalid SARIF schema"):
        service.sync_repository_execution(row, delivery_attempt_key="manual:invalid")

    assert db.query(Artifact).count() == 0


def test_create_run_queues_when_previous_run_finished_or_scope_differs(db):
    service = IacService(db)
    make_run(db, status="succeeded", scope="sandbox/portal")
    fresh = service.create_run(
        target_url="https://gitlab.enterprise.example.com/sandbox/portal",
        gitlab_base_url="https://gitlab.enterprise.example.com",
        scope_path="sandbox/portal",
    )
    other_scope = service.create_run(
        target_url="https://gitlab.enterprise.example.com/sandbox/other",
        gitlab_base_url="https://gitlab.enterprise.example.com",
        scope_path="sandbox/other",
    )
    assert fresh.status == "queued"
    assert other_scope.id != fresh.id
    assert db.query(IacScanRun).count() == 3


def test_single_repo_rescan_is_deduped_by_gitlab_project(db):
    service = IacService(db)
    product = Product(name="Platform", path="platform", gitlab_base_url="https://gitlab.enterprise.example.com")
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="sandbox/portal/app")
    db.add(repository)
    db.commit()
    parent = make_run(db, status="succeeded")
    row = make_repo_row(db, parent, product_id=product.id, repository_id=repository.id)
    first = service.create_single_repo_run(row)
    second = service.create_single_repo_run(row)
    assert second.id == first.id
    assert first.is_rescan
    # A group-run duplicate check must not swallow rescans and vice versa.
    group = service.create_run(
        target_url=parent.target_url,
        gitlab_base_url=parent.gitlab_base_url,
        scope_path=parent.scope_path,
    )
    assert group.id != first.id
    execution = db.query(Execution).filter(Execution.correlation_key == f"iac_run:{first.id}").one()
    assert execution.repository_id == repository.id
    assert execution.product_id == product.id


def test_latest_repo_rows_take_last_finished_run_of_any_outcome(db):
    service = IacService(db)
    ok_run = make_run(db, status="succeeded")
    make_repo_row(db, ok_run)
    partial_run = make_run(db, status="partial")
    make_repo_row(db, partial_run, critical_count=3)
    failed_run = make_run(db, status="failed")
    latest = make_repo_row(db, failed_run, scan_status="failed")
    running_run = make_run(db, status="running")
    make_repo_row(db, running_run, scan_status="queued")

    rows, total = service.latest_repo_rows()
    assert total == 1
    assert rows[0].id == latest.id
    assert rows[0].scan_status == "failed"


def test_latest_repo_rows_filter_by_search_status_and_important_findings(db):
    service = IacService(db)
    portal_run = make_run(db, scope="sandbox/portal")
    make_repo_row(
        db,
        portal_run,
        project_path="sandbox/portal/api",
        critical_count=1,
        high_count=2,
        scan_status="succeeded",
        asoc_upload_status="uploaded",
    )
    clean_run = make_run(db, scope="sandbox/clean")
    make_repo_row(
        db,
        clean_run,
        project_path="sandbox/clean/app",
        scan_status="no_iac",
        asoc_upload_status="skipped",
        has_iac=False,
    )

    searched, total = service.latest_repo_rows(query_text="PORTAL", iac="yes")
    assert total == 1
    assert [row.project_path for row in searched] == ["sandbox/portal/api"]

    no_iac, total = service.latest_repo_rows(scan_status="no_iac")
    assert total == 1
    assert [row.project_path for row in no_iac] == ["sandbox/clean/app"]

    skipped, total = service.latest_repo_rows(asoc_status="skipped")
    assert total == 1
    assert [row.project_path for row in skipped] == ["sandbox/clean/app"]

    important, total = service.latest_repo_rows(has_important_findings=True)
    assert total == 1
    assert [row.project_path for row in important] == ["sandbox/portal/api"]


def test_repository_scan_history_is_scoped_to_gitlab_instance_and_project(db):
    service = IacService(db)
    first_run = make_run(db, status="succeeded")
    first = make_repo_row(db, first_run, findings_count=1)
    second_run = make_run(db, status="succeeded", trigger="rescan")
    second = make_repo_row(db, second_run, findings_count=2)

    other_host_run = make_run(
        db,
        status="succeeded",
        gitlab_base_url="https://gitlab.example.com",
        target_url="https://gitlab.example.com/sandbox/portal/app",
    )
    make_repo_row(
        db,
        other_host_run,
        project_url="https://gitlab.example.com/sandbox/portal/app",
        findings_count=99,
    )
    other_project_run = make_run(db, status="succeeded")
    make_repo_row(db, other_project_run, gitlab_project_id=202, project_path="sandbox/portal/other")

    history = service.repository_scan_history(first)

    assert [row.id for row in history] == [second.id, first.id]
    assert [row.findings_count for row in history] == [2, 1]
    assert all(row.run is not None for row in history)


def test_latest_findings_page_returns_distinct_pages_for_latest_iac_rows(db):
    service = IacService(db)
    run = make_run(db)
    api = make_repo_row(db, run, project_path="sandbox/portal/api")
    worker = make_repo_row(db, run, project_path="sandbox/portal/worker", gitlab_project_id=102)
    findings = [
        IacScanFinding(
            run_id=run.id,
            scan_repository_id=api.id,
            project_path=api.project_path,
            project_url=api.project_url,
            severity="critical",
            rule_id="CKV_1",
            title="Critical issue",
        ),
        IacScanFinding(
            run_id=run.id,
            scan_repository_id=api.id,
            project_path=api.project_path,
            project_url=api.project_url,
            severity="high",
            rule_id="CKV_2",
            title="High issue",
        ),
        IacScanFinding(
            run_id=run.id,
            scan_repository_id=worker.id,
            project_path=worker.project_path,
            project_url=worker.project_url,
            severity="medium",
            rule_id="CKV_3",
            title="Medium issue",
            scanner_name="kics",
        ),
    ]
    db.add_all(findings)
    db.commit()

    first_page, total = service.latest_findings_page(limit=2, offset=0)
    second_page, repeated_total = service.latest_findings_page(limit=2, offset=2)

    assert total == 3
    assert repeated_total == 3
    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {finding.id for finding in first_page}.isdisjoint({finding.id for finding in second_page})

    filtered, filtered_total = service.latest_findings_page(
        severity="high",
        rule_query="CKV_2",
        project_query="portal/api",
        scanner_name="checkov",
    )
    assert filtered_total == 1
    assert [finding.rule_id for finding in filtered] == ["CKV_2"]


def test_cancel_queued_run_marks_it_terminal_and_idempotent(db):
    service = IacService(db)
    run = make_run(db, status="queued")

    cancelled = service.cancel_run(run.id)
    repeated = service.cancel_run(run.id)

    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested_at is not None
    assert cancelled.cancelled_at is not None
    assert repeated.id == run.id
    assert repeated.status == "cancelled"


def test_cancel_running_run_requests_cooperative_stop(db):
    service = IacService(db)
    run = make_run(db, status="running")

    requested = service.cancel_run(run.id)

    assert requested.status == "running"
    assert requested.cancel_requested_at is not None
    assert requested.cancelled_at is None


def test_cancel_rejects_other_terminal_run_states(db):
    service = IacService(db)
    run = make_run(db, status="succeeded")

    with pytest.raises(ValueError, match="cannot be cancelled"):
        service.cancel_run(run.id)
