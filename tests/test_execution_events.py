from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Artifact, ArtifactProvenance, Delivery, Execution, ExecutionEvent, ExecutionStep, Product, Repository
from app.execution_event_service import EventConflictError, ExecutionEventService
from app.schemas import ExecutionEventIngestRequest


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (
        Product.__table__,
        Repository.__table__,
        Execution.__table__,
        ExecutionStep.__table__,
        Artifact.__table__,
        Delivery.__table__,
        ArtifactProvenance.__table__,
        ExecutionEvent.__table__,
    ):
        table.create(engine)
    return sessionmaker(bind=engine)()


def _event(**overrides):
    data = {
        "schema_version": "1.0",
        "event_id": "evt-001",
        "event_type": "execution.transition",
        "source": "gitlab_adapter",
        "source_scope": "gitlab.example/platform",
        "correlation_key": "pipeline:42:100",
        "observed_at": datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc),
        "repository_id": None,
        "product_id": None,
        "reason": "pipeline accepted",
        "data": {"status": "running"},
    }
    data.update(overrides)
    return ExecutionEventIngestRequest(**data)


def test_replayed_event_is_idempotent_and_keeps_transition_context():
    db = _session()
    service = ExecutionEventService(db)

    first, replayed = service.ingest(_event())
    second, replayed_again = service.ingest(_event())

    assert replayed is False
    assert replayed_again is True
    assert second.id == first.id
    assert db.query(ExecutionEvent).count() == 1
    assert db.query(Execution).count() == 1
    assert first.source == "gitlab_adapter"
    assert first.reason == "pipeline accepted"
    assert first.observed_at.isoformat().startswith("2026-07-21T01:00:00")


def test_same_event_identity_with_different_payload_is_a_conflict():
    db = _session()
    service = ExecutionEventService(db)
    service.ingest(_event())

    with pytest.raises(EventConflictError):
        service.ingest(_event(data={"status": "failed"}, reason="scanner failed"))

    assert db.query(ExecutionEvent).count() == 1
    assert db.query(Execution).one().status == "running"


def test_event_identity_is_scoped_and_sensitive_payloads_are_rejected():
    db = _session()
    service = ExecutionEventService(db)
    first, _ = service.ingest(_event())
    second, _ = service.ingest(_event(source_scope="gitlab.example/other"))

    assert first.id != second.id
    assert db.query(ExecutionEvent).count() == 2
    assert db.query(Execution).count() == 2
    with pytest.raises(ValidationError, match="sensitive key"):
        _event(event_id="evt-secret", data={"status": "running", "authorization": "Bearer secret"})
    with pytest.raises(ValidationError, match="sensitive value"):
        _event(event_id="evt-reason-secret", reason="Authorization: Bearer top-secret")
    with pytest.raises(ValidationError, match="sensitive value"):
        _event(event_id="evt-error-secret", data={"status": "failed", "error_text": "Bearer nested-secret"})
    with pytest.raises(ValidationError, match="sensitive value"):
        _event(
            event_id="evt-url-secret",
            event_type="artifact.created",
            data={"uri": "https://service-user:service-password@reports.example/result.sarif"},
        )


def test_source_observation_time_must_include_timezone():
    with pytest.raises(ValidationError, match="timezone"):
        _event(observed_at=datetime(2026, 7, 21, 1, 0))


def test_step_artifact_and_delivery_events_project_into_shared_contracts():
    db = _session()
    service = ExecutionEventService(db)
    service.ingest(_event())
    service.ingest(
        _event(
            event_id="evt-002",
            event_type="step.transition",
            data={"external_id": "job:7", "name": "SAST", "status": "succeeded"},
        )
    )
    service.ingest(
        _event(
            event_id="evt-003",
            event_type="artifact.created",
            data={
                "external_id": "sarif:7",
                "kind": "sarif",
                "uri": "reports/7.sarif",
                "digest_sha256": "a" * 64,
                "size_bytes": 128,
                "schema_version": "2.1.0",
                "source_revision": "deadbeef",
                "source_ref": "main",
            },
        )
    )
    service.ingest(
        _event(
            event_id="evt-004",
            event_type="delivery.transition",
            data={
                "artifact_external_id": "sarif:7",
                "destination": "asoc",
                "attempt_key": "ci:100",
                "status": "confirmed",
                "external_id": "asoc:report:7",
            },
        )
    )

    assert db.query(ExecutionStep).one().status == "succeeded"
    assert db.query(Artifact).one().digest_sha256 == "a" * 64
    assert db.query(ArtifactProvenance).one().source_revision == "deadbeef"
    assert db.query(Delivery).one().status == "confirmed"


def test_event_history_is_ordered_and_filterable():
    db = _session()
    service = ExecutionEventService(db)
    service.ingest(_event(event_id="evt-001"))
    service.ingest(
        _event(
            event_id="evt-002",
            event_type="execution.transition",
            observed_at=datetime(2026, 7, 21, 1, 1, tzinfo=timezone.utc),
            data={"status": "succeeded"},
        )
    )

    history = service.history(source="gitlab_adapter", correlation_key="pipeline:42:100")

    assert [item["event_id"] for item in history] == ["evt-002", "evt-001"]
    assert history[0]["data"] == {"status": "succeeded"}
    assert history[0]["observed_at"].startswith("2026-07-21T01:01:00")


def test_concurrent_event_insert_recovers_the_existing_event(monkeypatch):
    db = _session()
    service = ExecutionEventService(db)
    existing, _ = service.ingest(_event())
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
        if miss_once["value"] and entities == (ExecutionEvent,):
            return _Miss()
        return real_query(*entities, **kwargs)

    monkeypatch.setattr(db, "query", query_with_stale_first_read)

    replay, replayed = service.ingest(_event())

    assert replayed is True
    assert replay.id == existing.id
    assert real_query(ExecutionEvent).count() == 1


def test_deleting_projection_preserves_immutable_event_history():
    db = _session()
    event, _ = ExecutionEventService(db).ingest(_event())
    db.commit()
    execution = db.query(Execution).one()

    db.delete(execution)
    db.commit()

    preserved = db.query(ExecutionEvent).one()
    assert preserved.id == event.id
    assert preserved.execution_id is None


def test_versioned_ingestion_endpoint_returns_201_then_200(monkeypatch):
    monkeypatch.setenv("UI_AUTH_USERNAME", "devsec")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "secret")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "ci-token")
    db = _session()
    from app.main import app
    from app.routers import api as api_routes

    def override_db():
        yield db

    app.dependency_overrides[api_routes.get_db] = override_db
    client = TestClient(app)
    payload = _event().model_dump(mode="json")
    try:
        first = client.post("/api/v1/execution-events", json=payload, headers={"Authorization": "Bearer ci-token"})
        replay = client.post("/api/v1/execution-events", json=payload, headers={"Authorization": "Bearer ci-token"})
        conflict_payload = {**payload, "data": {"status": "failed"}}
        conflict = client.post(
            "/api/v1/execution-events", json=conflict_payload, headers={"Authorization": "Bearer ci-token"}
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 201
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 409
