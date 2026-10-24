"""Synchronize safe operational connector and scanner-instance snapshots."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .integration_settings import (
    CONNECTOR_SPECS,
    RuntimeConnector,
    _gitlab_instances_for_snapshot,
    connector_is_configured,
    load_runtime_connector,
)
from .models import (
    Connector,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
    PipelineJob,
    ScannerInstance,
    WorkerHeartbeat,
)

STALE_AFTER = timedelta(minutes=30)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def worker_observations(db: Session) -> dict[str, datetime]:
    """Return the last heartbeat per worker; missing legacy tables are no data."""
    if not inspect(db.bind).has_table(WorkerHeartbeat.__tablename__):
        return {}
    try:
        return {row.name: _utc(row.last_beat_at) for row in db.query(WorkerHeartbeat).all() if row.last_beat_at}
    except SQLAlchemyError:
        db.rollback()
        return {}


def _endpoint(runtime: RuntimeConnector) -> str | None:
    if runtime.key == "mattermost":
        return urlparse(runtime.get("webhook_url").strip()).netloc or None
    for field in ("api_url", "url", "webhook_url"):
        value = runtime.get(field).strip()
        if value:
            return value
    return None


def _credential_ref(runtime: RuntimeConnector) -> str:
    return f"database://integration-configs/{runtime.key}" if runtime.source == "database" else "environment fallback"


def _latest_connector_tests(
    db: Session,
    connector_keys: list[str],
) -> dict[str, tuple[str, str | None]]:
    if not inspect(db.bind).has_table(IntegrationConfigEvent.__tablename__):
        return {}
    result: dict[str, tuple[str, str | None]] = {}
    for connector in connector_keys:
        event = (
            db.query(IntegrationConfigEvent)
            .filter(
                IntegrationConfigEvent.connector == connector,
                IntegrationConfigEvent.action == "test",
            )
            .order_by(IntegrationConfigEvent.id.desc())
            .first()
        )
        if event is not None:
            result[connector] = (
                "healthy" if event.success else "failed",
                event.message,
            )
    return result


def _instance_test_states(
    db: Session,
    connector: str,
) -> dict[str, IntegrationInstanceTestState]:
    if not inspect(db.bind).has_table(IntegrationInstanceTestState.__tablename__):
        return {}
    return {
        state.instance_id: state
        for state in (
            db.query(IntegrationInstanceTestState)
            .filter(IntegrationInstanceTestState.connector == connector)
            .all()
        )
    }


def _latest_gitlab_observation(db: Session) -> datetime | None:
    """Return the latest persisted job observation from a successful poll.

    Connector health checks and pipeline polling are separate signals.  The
    fleet connector's ``last_seen_at`` must describe GitLab data that was
    actually received, not an attempted request that may have failed. A
    ``PipelineJob.last_seen_at`` value is written only after GitLab returned a
    job payload successfully. Older test databases may not have the job table,
    therefore the
    projection treats that case as no signal instead of failing the registry.
    """
    if not inspect(db.bind).has_table(PipelineJob.__tablename__):
        return None
    try:
        return _utc(db.query(func.max(PipelineJob.last_seen_at)).scalar())
    except SQLAlchemyError:
        db.rollback()
        return None


def _runtime_health(
    runtime: RuntimeConnector,
    *,
    latest_test: tuple[str, str | None] | None = None,
    instance_states: dict[str, IntegrationInstanceTestState] | None = None,
) -> tuple[str, str]:
    if not runtime.enabled:
        return "disabled", "Connector is disabled"
    if runtime.error:
        return "degraded", runtime.error
    if not connector_is_configured(runtime):
        return "degraded", "Credential or required endpoint is missing"
    if runtime.key == "gitlab":
        entries = [
            entry
            for entry in _gitlab_instances_for_snapshot(runtime)
            if entry["enabled"] and entry["token_configured"]
        ]
        states = instance_states or {}
        failed = [
            states[entry["id"]]
            for entry in entries
            if entry["id"] in states
            and states[entry["id"]].test_status == "failed"
        ]
        if failed:
            return "degraded", failed[0].test_message or "A GitLab connection test failed"
        if entries and all(
            entry["id"] in states
            and states[entry["id"]].test_status == "healthy"
            for entry in entries
        ):
            return "healthy", f"Connection tests passed for {len(entries)} GitLab instance(s)"

    if runtime.row is not None and runtime.row.test_status:
        test_status, test_message = runtime.row.test_status, runtime.row.test_message
    else:
        test_status, test_message = latest_test or (None, None)
    if test_status == "failed":
        return "degraded", test_message or "Latest connection test failed"
    if test_status == "healthy":
        return "healthy", test_message or "Latest connection test passed"
    return "unknown", "Configured · not tested"


def _upsert_connector(
    db: Session,
    *,
    key: str,
    name: str,
    connector_type: str,
    endpoint: str | None,
    credential_ref: str | None,
    enabled: bool,
    health_status: str,
    health_reason: str,
    last_seen_at: datetime | None,
    now: datetime,
) -> Connector:
    row = db.query(Connector).filter(Connector.key == key).first()
    if row is None:
        row = Connector(key=key, name=name, connector_type=connector_type)
    row.name = name
    row.connector_type = connector_type
    row.endpoint = endpoint
    row.credential_ref = credential_ref
    row.enabled = enabled
    row.health_status = health_status
    row.health_reason = health_reason
    row.last_seen_at = last_seen_at
    row.refreshed_at = now
    db.add(row)
    db.flush()
    return row


def _upsert_instance(
    db: Session,
    connector: Connector,
    *,
    instance_key: str,
    name: str,
    instance_type: str,
    endpoint: str | None,
    health_status: str,
    health_reason: str,
    last_seen_at: datetime | None,
    capabilities: list[str],
) -> ScannerInstance:
    row = db.query(ScannerInstance).filter(ScannerInstance.instance_key == instance_key).first()
    if row is None:
        row = ScannerInstance(instance_key=instance_key, name=name, instance_type=instance_type)
    row.connector = connector
    row.name = name
    row.instance_type = instance_type
    row.endpoint = endpoint
    row.health_status = health_status
    row.health_reason = health_reason
    row.last_seen_at = last_seen_at
    row.capabilities_json = json.dumps(capabilities)
    db.add(row)
    db.flush()
    return row


def _serialize_connector(row: Connector) -> dict[str, Any]:
    return {
        "key": row.key,
        "name": row.name,
        "type": row.connector_type,
        "endpoint": row.endpoint,
        "credential_ref": row.credential_ref,
        "enabled": row.enabled,
        "health_status": row.health_status,
        "health_reason": row.health_reason,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _serialize_instance(row: ScannerInstance) -> dict[str, Any]:
    return {
        "instance_key": row.instance_key,
        "connector_key": row.connector.key,
        "name": row.name,
        "type": row.instance_type,
        "endpoint": row.endpoint,
        "version": row.version,
        "capabilities": json.loads(row.capabilities_json or "[]"),
        "health_status": row.health_status,
        "health_reason": row.health_reason,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _reconcile_gitlab_instances(
    db: Session,
    connector: Connector,
    current_instance_keys: set[str],
) -> None:
    """Remove legacy, deleted and renamed GitLab instance projections."""
    stale = db.query(ScannerInstance).filter(
        ScannerInstance.connector_id == connector.id,
        ScannerInstance.instance_key.like("gitlab:%"),
    )
    if current_instance_keys:
        stale = stale.filter(
            ~ScannerInstance.instance_key.in_(sorted(current_instance_keys))
        )
    stale.delete(synchronize_session=False)


def refresh_registry(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Project configured connectors and live worker observations into one view."""
    now = _utc(now) or datetime.now(timezone.utc)
    connectors: list[Connector] = []
    instances: list[ScannerInstance] = []
    beats = worker_observations(db)
    latest_tests = _latest_connector_tests(db, list(CONNECTOR_SPECS))
    gitlab_instance_states = _instance_test_states(db, "gitlab")
    for key, spec in CONNECTOR_SPECS.items():
        runtime = load_runtime_connector(key, db)
        status, reason = _runtime_health(
            runtime,
            latest_test=latest_tests.get(key),
            instance_states=gitlab_instance_states if key == "gitlab" else None,
        )
        connector = _upsert_connector(
            db,
            key=key,
            name=str(spec["name"]),
            connector_type=key,
            endpoint=_endpoint(runtime),
            credential_ref=_credential_ref(runtime),
            enabled=runtime.enabled,
            health_status=status,
            health_reason=reason,
            last_seen_at=_latest_gitlab_observation(db) if key == "gitlab" else None,
            now=now,
        )
        connectors.append(connector)
        if key == "gitlab":
            current_gitlab_keys: set[str] = set()
            for entry in _gitlab_instances_for_snapshot(runtime):
                instance_key = f"gitlab:{entry['id']}"
                current_gitlab_keys.add(instance_key)
                instance_state = gitlab_instance_states.get(entry["id"])
                if not runtime.enabled or not entry["enabled"]:
                    instance_status = "disabled"
                    instance_reason = "GitLab instance is disabled"
                    instance_seen_at = None
                elif not entry["token_configured"]:
                    instance_status = "degraded"
                    instance_reason = "Credential is missing"
                    instance_seen_at = None
                elif instance_state is not None:
                    instance_status = instance_state.test_status
                    instance_reason = (
                        instance_state.test_message
                        or "Latest GitLab connection test has no detail"
                    )
                    instance_seen_at = (
                        instance_state.tested_at
                        if instance_state.test_status == "healthy"
                        else None
                    )
                else:
                    instance_status = "unknown"
                    instance_reason = "Configured · not tested"
                    instance_seen_at = None
                instances.append(
                    _upsert_instance(
                        db,
                        connector,
                        instance_key=instance_key,
                        name=entry["name"],
                        instance_type="scanner",
                        endpoint=entry["api_url"],
                        health_status=instance_status,
                        health_reason=instance_reason,
                        last_seen_at=instance_seen_at,
                        capabilities=["pipeline registration", "job polling", "repository discovery"],
                    )
                )
            _reconcile_gitlab_instances(db, connector, current_gitlab_keys)
        else:
            instances.append(
                _upsert_instance(
                    db,
                    connector,
                    instance_key=f"connector:{key}",
                    name=str(spec["name"]),
                    instance_type="scanner",
                    endpoint=_endpoint(runtime),
                    health_status=status,
                    health_reason=reason,
                    last_seen_at=now if status == "healthy" else None,
                    capabilities=[],
                )
            )

    if beats:
        stale_workers = [last_seen_at for last_seen_at in beats.values() if now - last_seen_at > STALE_AFTER]
        worker_status = "stale" if stale_workers else "healthy"
        worker_connector = _upsert_connector(
            db,
            key="workers",
            name="ScanOps workers",
            connector_type="worker",
            endpoint=None,
            credential_ref=None,
            enabled=True,
            health_status=worker_status,
            health_reason="Worker heartbeats are stale" if stale_workers else "Worker heartbeats are available",
            last_seen_at=max(beats.values()) if beats else None,
            now=now,
        )
        connectors.append(worker_connector)
        for name, last_seen_at in beats.items():
            stale = now - last_seen_at > STALE_AFTER
            instances.append(
                _upsert_instance(
                    db,
                    worker_connector,
                    instance_key=f"worker:{name}",
                    name=name.replace("_", " ").title(),
                    instance_type="worker",
                    endpoint=None,
                    health_status="stale" if stale else "healthy",
                    health_reason="Last heartbeat is stale" if stale else "Recent heartbeat received",
                    last_seen_at=last_seen_at,
                    capabilities=["background execution"],
                )
            )
    db.commit()

    enabled = [row for row in connectors if row.enabled]
    healthy = [row for row in enabled if row.health_status == "healthy"]
    return {
        "connectors": [_serialize_connector(row) for row in connectors],
        "instances": [_serialize_instance(row) for row in instances],
        "success_rate": round((len(healthy) / len(enabled)) * 100) if enabled else 100,
        "healthy_connectors": len(healthy),
        "enabled_connectors": len(enabled),
    }
