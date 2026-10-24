from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.integration_settings import RuntimeConnector
from app.models import (
    ActivePipeline,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
    PipelineJob,
    PipelineStatus,
    Product,
    Repository,
)
from app.models.connector_registry import Connector, ScannerInstance


def _session():
    engine = create_engine("sqlite:///:memory:")
    Product.__table__.create(engine)
    Repository.__table__.create(engine)
    ActivePipeline.__table__.create(engine)
    PipelineJob.__table__.create(engine)
    Connector.__table__.create(engine)
    ScannerInstance.__table__.create(engine)
    IntegrationConfigEvent.__table__.create(engine)
    IntegrationInstanceTestState.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_registry_uses_latest_successful_gitlab_job_observation_as_connector_signal(monkeypatch):
    from app import connector_registry

    db = _session()
    observed_at = datetime(2026, 7, 20, 11, 42, tzinfo=timezone.utc)
    pipeline = ActivePipeline(
        project_id=1,
        pipeline_id=42,
        repository_id=1,
        product_id=1,
        status=PipelineStatus.POLLING,
        last_polled_at=observed_at + timedelta(minutes=17),
        error_message="GitLab request timed out",
    )
    db.add(pipeline)
    db.flush()
    db.add(
        PipelineJob(
            active_pipeline_id=pipeline.id,
            gitlab_job_id=4201,
            name="sast_scan",
            status="running",
            last_seen_at=observed_at,
        )
    )
    db.commit()
    runtime = RuntimeConnector(
        key="gitlab",
        values={"api_url": "https://gitlab.example/api/v4"},
        secrets={"token": "not-for-output"},
        enabled=True,
        source="environment",
    )
    monkeypatch.setattr(
        connector_registry,
        "CONNECTOR_SPECS",
        {"gitlab": {"name": "GitLab"}},
    )
    monkeypatch.setattr(
        connector_registry,
        "load_runtime_connector",
        lambda key, db: runtime,
    )

    snapshot = connector_registry.refresh_registry(
        db,
        now=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )

    assert snapshot["connectors"][0]["last_seen_at"] == observed_at.replace(
        tzinfo=None
    ).isoformat()
    assert db.query(Connector).one().last_seen_at == observed_at.replace(tzinfo=None)


def test_registry_marks_untested_gitlab_instances_unknown(monkeypatch):
    from app import connector_registry

    db = _session()
    runtime = RuntimeConnector(
        key="gitlab",
        values={"api_url": "https://gitlab.example/api/v4"},
        secrets={"token": "not-for-output"},
        enabled=True,
        source="environment",
    )
    monkeypatch.setattr(connector_registry, "CONNECTOR_SPECS", {"gitlab": {"name": "GitLab"}})
    monkeypatch.setattr(connector_registry, "load_runtime_connector", lambda key, db: runtime)

    snapshot = connector_registry.refresh_registry(db, now=datetime(2026, 7, 20, tzinfo=timezone.utc))

    assert snapshot["success_rate"] == 0
    assert snapshot["connectors"][0]["credential_ref"] == "environment fallback"
    assert snapshot["instances"][0]["health_status"] == "unknown"
    assert snapshot["instances"][0]["health_reason"] == "Configured · not tested"
    assert "not-for-output" not in str(snapshot)


def test_registry_marks_missing_credentials_degraded_and_disabled_excluded(monkeypatch):
    from app import connector_registry

    db = _session()
    runtimes = {
        "asoc": RuntimeConnector("asoc", {"url": "https://asoc.example"}, {}, True, "environment"),
        "ptai": RuntimeConnector("ptai", {}, {}, False, "database"),
    }
    monkeypatch.setattr(
        connector_registry,
        "CONNECTOR_SPECS",
        {"asoc": {"name": "ASOC"}, "ptai": {"name": "PT AI"}},
    )
    monkeypatch.setattr(connector_registry, "load_runtime_connector", lambda key, db: runtimes[key])

    snapshot = connector_registry.refresh_registry(db, now=datetime(2026, 7, 20, tzinfo=timezone.utc))

    by_key = {item["key"]: item for item in snapshot["connectors"]}
    assert by_key["asoc"]["health_status"] == "degraded"
    assert by_key["ptai"]["health_status"] == "disabled"
    assert snapshot["success_rate"] == 0


def test_registry_uses_latest_failed_connection_test(monkeypatch):
    from app import connector_registry

    db = _session()
    runtime = RuntimeConnector(
        "asoc",
        {"url": "https://asoc.example"},
        {"token": "secret"},
        True,
        "environment",
    )
    db.add(
        IntegrationConfigEvent(
            connector="asoc",
            action="test",
            actor="operator",
            changed_fields_json="[]",
            success=False,
            message="TLS verification failed",
        )
    )
    db.commit()
    monkeypatch.setattr(
        connector_registry, "CONNECTOR_SPECS", {"asoc": {"name": "ASOC"}}
    )
    monkeypatch.setattr(
        connector_registry,
        "load_runtime_connector",
        lambda key, db: runtime,
    )

    snapshot = connector_registry.refresh_registry(
        db, now=datetime(2026, 7, 20, tzinfo=timezone.utc)
    )

    connector = snapshot["connectors"][0]
    assert connector["health_status"] == "degraded"
    assert connector["health_reason"] == "TLS verification failed"
    assert snapshot["success_rate"] == 0


def test_registry_reconciles_legacy_and_deleted_gitlab_instance_rows(monkeypatch):
    from app import connector_registry

    db = _session()
    connector = Connector(
        key="gitlab",
        name="GitLab",
        connector_type="gitlab",
        enabled=True,
        health_status="healthy",
    )
    db.add(connector)
    db.flush()
    db.add_all(
        [
            ScannerInstance(
                connector=connector,
                instance_key="gitlab:gitlab.example",
                name="Legacy host key",
                instance_type="scanner",
                health_status="healthy",
            ),
            ScannerInstance(
                connector=connector,
                instance_key="gitlab:gl-0000000000000000",
                name="Deleted instance",
                instance_type="scanner",
                health_status="failed",
            ),
        ]
    )
    db.commit()
    runtime = RuntimeConnector(
        key="gitlab",
        values={"api_url": "https://gitlab.example/api/v4"},
        secrets={"token": "not-for-output"},
        enabled=True,
        source="environment",
    )
    monkeypatch.setattr(
        connector_registry, "CONNECTOR_SPECS", {"gitlab": {"name": "GitLab"}}
    )
    monkeypatch.setattr(
        connector_registry,
        "load_runtime_connector",
        lambda key, db: runtime,
    )

    snapshot = connector_registry.refresh_registry(
        db, now=datetime(2026, 7, 20, tzinfo=timezone.utc)
    )

    projected_keys = [
        row.instance_key
        for row in db.query(ScannerInstance).order_by(ScannerInstance.instance_key)
    ]
    assert projected_keys == [snapshot["instances"][0]["instance_key"]]
    assert projected_keys[0].startswith("gitlab:gl-")
    assert "gitlab:gitlab.example" not in projected_keys
    assert "gitlab:gl-0000000000000000" not in projected_keys


def test_registry_redacts_mattermost_webhook_from_payload_and_persistence(monkeypatch):
    from app import connector_registry

    db = _session()
    webhook = "https://mattermost.example/hooks/secret-webhook-token"
    runtime = RuntimeConnector("mattermost", {}, {"webhook_url": webhook}, True, "environment")
    monkeypatch.setattr(connector_registry, "CONNECTOR_SPECS", {"mattermost": {"name": "Mattermost"}})
    monkeypatch.setattr(connector_registry, "load_runtime_connector", lambda key, db: runtime)

    snapshot = connector_registry.refresh_registry(db, now=datetime(2026, 7, 20, tzinfo=timezone.utc))

    assert webhook not in str(snapshot)
    assert "secret-webhook-token" not in str(snapshot)
    assert snapshot["connectors"][0]["endpoint"] == "mattermost.example"
    assert db.query(Connector).one().endpoint == "mattermost.example"


def test_registry_marks_old_worker_heartbeat_stale(monkeypatch):
    from app import connector_registry

    db = _session()
    runtime = RuntimeConnector("gitlab", {"api_url": "https://gitlab.example/api/v4"}, {"token": "x"}, True, "environment")
    monkeypatch.setattr(connector_registry, "CONNECTOR_SPECS", {"gitlab": {"name": "GitLab"}})
    monkeypatch.setattr(connector_registry, "load_runtime_connector", lambda key, db: runtime)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(connector_registry, "worker_observations", lambda db: {"pipeline_worker": now - timedelta(hours=2)})

    snapshot = connector_registry.refresh_registry(db, now=now)

    worker = next(item for item in snapshot["instances"] if item["instance_key"] == "worker:pipeline_worker")
    assert worker["health_status"] == "stale"
    assert worker["health_reason"] == "Last heartbeat is stale"
    workers = next(item for item in snapshot["connectors"] if item["key"] == "workers")
    assert workers["health_status"] == "stale"
    assert snapshot["success_rate"] == 0
