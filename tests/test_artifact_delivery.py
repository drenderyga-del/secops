from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.artifact_delivery_service import ArtifactDeliveryService, DeliveryStatus
from app.execution_contract import ExecutionStatus
from app.execution_service import ExecutionService
from app.models.artifact import Artifact, ArtifactProvenance, Delivery
from app.models.execution import Execution, ExecutionEvent, ExecutionStep


def _session():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        Execution.__table__,
        ExecutionStep.__table__,
        ExecutionEvent.__table__,
        Artifact.__table__,
        Delivery.__table__,
        ArtifactProvenance.__table__,
    ):
        table.create(engine)
    return sessionmaker(bind=engine)()


def _execution(db):
    return ExecutionService(db).observe_execution(
        source="iac_repository",
        correlation_key="iac_repository:9:101",
        repository_id=7,
        product_id=3,
        status=ExecutionStatus.RUNNING,
        observed_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )


def test_artifact_provenance_delivery_replay_and_success_policy():
    db = _session()
    execution = _execution(db)
    service = ArtifactDeliveryService(db)
    observed_at = datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc)

    artifact = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="run-9-repo-101-sarif",
        kind="sarif",
        uri="/reports/run-9-repo-101.sarif",
        digest_sha256="a" * 64,
        size_bytes=128,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    replay = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="run-9-repo-101-sarif",
        kind="sarif",
        uri="/reports/run-9-repo-101.sarif",
        digest_sha256="a" * 64,
        size_bytes=128,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    provenance = service.record_provenance(
        artifact=artifact,
        execution=execution,
        source_revision="deadbeef",
        source_ref="main",
        observed_at=observed_at,
    )
    delivery = service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc:repository:abc",
        status=DeliveryStatus.CONFIRMED,
        external_id="abc",
        observed_at=observed_at,
    )
    replayed_delivery = service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc:repository:abc",
        status=DeliveryStatus.CONFIRMED,
        external_id="abc",
        observed_at=observed_at + timedelta(minutes=5),
    )

    finalized = service.finalize_execution(
        execution,
        delivery_required=True,
        required_delivery_source="asoc",
        observed_at=observed_at,
    )

    assert replay.id == artifact.id
    assert provenance.artifact_id == artifact.id
    assert replayed_delivery.id == delivery.id
    assert replayed_delivery.confirmed_at == observed_at
    assert finalized.status == ExecutionStatus.SUCCEEDED.value


def test_required_delivery_without_confirmation_fails_closed():
    db = _session()
    execution = _execution(db)
    service = ArtifactDeliveryService(db)
    observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    artifact = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="artifact:1",
        kind="sarif",
        uri="/reports/one.sarif",
        digest_sha256="b" * 64,
        size_bytes=32,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc:repository:abc",
        status=DeliveryStatus.SKIPPED,
        external_id=None,
        observed_at=observed_at,
    )

    finalized = service.finalize_execution(
        execution,
        delivery_required=True,
        required_delivery_source="asoc",
        observed_at=observed_at,
    )

    assert finalized.status == ExecutionStatus.FAILED.value
    assert finalized.failure_category == "delivery"
    assert finalized.failure_reason == "confirmation_missing"


def test_not_applicable_control_succeeds_without_artifact():
    db = _session()
    execution = _execution(db)

    finalized = ArtifactDeliveryService(db).finalize_execution(
        execution,
        delivery_required=False,
        not_applicable=True,
        observed_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert finalized.status == ExecutionStatus.SUCCEEDED.value
    assert db.query(Artifact).count() == 0


def test_delivery_and_finalization_forward_source_scope_when_execution_contract_supports_it(monkeypatch):
    db = _session()
    execution = _execution(db)
    execution.source_scope = "tenant-a"
    artifact = ArtifactDeliveryService(db).record_artifact(
        execution=execution,
        source="checkov",
        external_id="artifact:scoped",
        kind="sarif",
        uri="/reports/scoped.sarif",
        digest_sha256="e" * 64,
        size_bytes=64,
        schema_version="2.1.0",
        observed_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    observations = []

    def observe_execution(_self, **kwargs):
        observations.append(kwargs)
        return execution

    monkeypatch.setattr(ExecutionService, "observe_execution", observe_execution)
    service = ArtifactDeliveryService(db)
    service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc:scoped",
        status=DeliveryStatus.RUNNING,
        external_id=None,
        observed_at=datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc),
    )
    service.finalize_execution(
        execution,
        delivery_required=False,
        observed_at=datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc),
    )

    assert [item["source_scope"] for item in observations] == ["tenant-a", "tenant-a"]


def test_confirmed_delivery_cannot_regress():
    db = _session()
    execution = _execution(db)
    service = ArtifactDeliveryService(db)
    observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    artifact = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="artifact:2",
        kind="sarif",
        uri="/reports/two.sarif",
        digest_sha256="c" * 64,
        size_bytes=64,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc:repository:abc",
        status=DeliveryStatus.CONFIRMED,
        external_id="abc",
        observed_at=observed_at,
    )

    with pytest.raises(ValueError, match="Invalid delivery transition"):
        service.observe_delivery(
            artifact=artifact,
            source="asoc",
            destination="asoc:repository:abc",
            status=DeliveryStatus.RUNNING,
            external_id="abc",
            observed_at=observed_at,
        )


def test_new_delivery_attempt_preserves_previous_failed_attempt():
    db = _session()
    execution = _execution(db)
    service = ArtifactDeliveryService(db)
    observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    artifact = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="artifact:retry",
        kind="sarif",
        uri="/reports/retry.sarif",
        digest_sha256="f" * 64,
        size_bytes=64,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    first = service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc",
        status=DeliveryStatus.FAILED,
        external_id=None,
        attempt_key="initial",
        observed_at=observed_at,
    )
    retry = service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc",
        status=DeliveryStatus.CONFIRMED,
        external_id="asoc-42",
        attempt_key="manual:1",
        observed_at=observed_at + timedelta(minutes=5),
    )

    assert first.id != retry.id
    assert first.status == DeliveryStatus.FAILED.value
    assert retry.status == DeliveryStatus.CONFIRMED.value
    assert db.query(Delivery).count() == 2


def test_non_asoc_confirmation_does_not_satisfy_required_asoc_delivery():
    db = _session()
    execution = _execution(db)
    service = ArtifactDeliveryService(db)
    observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    artifact = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="artifact:archive-only",
        kind="sarif",
        uri="/reports/archive.sarif",
        digest_sha256="3" * 64,
        size_bytes=64,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    service.observe_delivery(
        artifact=artifact,
        source="s3",
        destination="archive",
        status=DeliveryStatus.CONFIRMED,
        external_id="archive-1",
        observed_at=observed_at,
    )

    finalized = service.finalize_execution(
        execution,
        delivery_required=True,
        required_delivery_source="asoc",
        observed_at=observed_at,
    )

    assert finalized.status == ExecutionStatus.FAILED.value
    assert finalized.failure_reason == "confirmation_missing"


def test_concurrent_provenance_replay_returns_existing_link(monkeypatch):
    db = _session()
    execution = _execution(db)
    service = ArtifactDeliveryService(db)
    observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    artifact = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="artifact:provenance-race",
        kind="sarif",
        uri="/reports/race.sarif",
        digest_sha256="2" * 64,
        size_bytes=64,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    existing = service.record_provenance(
        artifact=artifact,
        execution=execution,
        source_revision="deadbeef",
        source_ref="main",
        observed_at=observed_at,
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
        if miss_once["value"] and entities == (ArtifactProvenance,):
            return _Miss()
        return real_query(*entities, **kwargs)

    monkeypatch.setattr(db, "query", query_with_stale_first_read)

    replay = service.record_provenance(
        artifact=artifact,
        execution=execution,
        source_revision="deadbeef",
        source_ref="main",
        observed_at=observed_at,
    )

    assert replay.id == existing.id


def test_repository_timeline_includes_artifact_provenance_and_delivery():
    db = _session()
    execution = _execution(db)
    service = ArtifactDeliveryService(db)
    observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    artifact = service.record_artifact(
        execution=execution,
        source="checkov",
        external_id="artifact:3",
        kind="sarif",
        uri="/reports/three.sarif",
        digest_sha256="d" * 64,
        size_bytes=96,
        schema_version="2.1.0",
        observed_at=observed_at,
    )
    service.record_provenance(
        artifact=artifact,
        execution=execution,
        source_revision="cafebabe",
        source_ref="main",
        observed_at=observed_at,
    )
    service.observe_delivery(
        artifact=artifact,
        source="asoc",
        destination="asoc:repository:def",
        status=DeliveryStatus.CONFIRMED,
        external_id="def",
        observed_at=observed_at,
    )
    db.commit()

    item = ExecutionService(db).repository_timeline(7)[0]

    assert item["artifacts"][0]["digest_sha256"] == "d" * 64
    assert item["artifacts"][0]["provenance"]["source_revision"] == "cafebabe"
    assert item["artifacts"][0]["deliveries"][0]["status"] == "confirmed"
