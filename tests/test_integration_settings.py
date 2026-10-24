from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.integration_settings import (
    CredentialStoreUnavailable,
    IntegrationConfigError,
    RuntimeConnector,
    connector_snapshot,
    disable_connector,
    load_runtime_connector,
    migrate_integration_credentials,
    record_test_result,
    save_connector,
    test_connector_connection as check_connector_connection,
)
from app.models import (
    IntegrationConfig,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
)
from app.routers.web import integrations as integrations_web
from app.routers.web.integrations import _redirect


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    IntegrationConfig.__table__.create(engine)
    IntegrationConfigEvent.__table__.create(engine)
    IntegrationInstanceTestState.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_ui_credentials_are_encrypted_and_never_returned(monkeypatch):
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "test-bootstrap-key-with-enough-entropy")
    db = _session()

    row = save_connector(
        db,
        "asoc",
        {
            "url": "https://asoc.example/api",
            "token": "top-secret-token",
            "space_id": "space-1",
        },
        actor="operator",
    )

    assert "top-secret-token" not in row.secrets_encrypted
    runtime = load_runtime_connector("asoc", db)
    assert runtime.get("token") == "top-secret-token"

    snapshot = connector_snapshot(db, "asoc")
    token_field = next(field for field in snapshot["fields"] if field["name"] == "token")
    assert token_field["value"] == ""
    assert token_field["configured"] is True
    assert snapshot["credential_ref"] == "database://integration-configs/asoc"

    save_connector(
        db,
        "asoc",
        {"url": "https://asoc-new.example/api", "token": "", "space_id": "space-1"},
        actor="operator",
    )
    assert load_runtime_connector("asoc", db).get("token") == "top-secret-token"
    events = db.query(IntegrationConfigEvent).all()
    assert all("top-secret-token" not in event.changed_fields_json for event in events)
    assert all("top-secret-token" not in (event.message or "") for event in events)


def test_single_gitlab_save_ignores_empty_environment_instance_placeholder(
    monkeypatch,
):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "managed-single-instance-key")
    monkeypatch.setenv("GITLAB_INSTANCES", "[]")
    monkeypatch.delenv("GITLAB_PAT", raising=False)
    monkeypatch.delenv("GITLAB_API_URL", raising=False)
    db = _session()

    save_connector(
        db,
        "gitlab",
        {
            "api_url": "https://gitlab.managed.example/api/v4",
            "token": "managed-secret",
            "instances_json": "",
        },
        actor="operator",
    )

    snapshot = connector_snapshot(db, "gitlab")
    assert snapshot["source"] == "database"
    assert snapshot["configured"] is True
    assert len(snapshot["instances"]) == 1
    assert snapshot["instances"][0]["api_url"] == (
        "https://gitlab.managed.example/api/v4"
    )
    assert "managed-secret" not in str(snapshot)


def test_environment_test_metadata_uses_events_without_copying_configuration(monkeypatch):
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.env.example/api/v4")
    monkeypatch.setenv("GITLAB_PAT", "environment-only-secret")
    monkeypatch.delenv("GITLAB_INSTANCES", raising=False)
    db = _session()

    expected = (
        (
            True,
            "Connected · authentication and project discovery verified",
            "healthy",
        ),
        (False, "Connection test failed: endpoint is unreachable", "failed"),
        (True, "Connected · recovered", "healthy"),
    )
    for success, message, status in expected:
        record_test_result(
            db,
            "gitlab",
            actor="operator",
            success=success,
            message=message,
        )
        snapshot = connector_snapshot(db, "gitlab")
        assert snapshot["source"] == "environment"
        assert snapshot["configured"] is True
        assert snapshot["test_status"] == status
        assert snapshot["test_message"] == message
        assert snapshot["tested_at"] is not None

    assert db.query(IntegrationConfig).count() == 0
    events = db.query(IntegrationConfigEvent).order_by(IntegrationConfigEvent.id).all()
    assert [event.success for event in events] == [True, False, True]
    assert "environment-only-secret" not in str(
        [
            (
                event.changed_fields_json,
                event.message,
            )
            for event in events
        ]
    )
    assert load_runtime_connector("gitlab", db).get("token") == "environment-only-secret"


def test_connector_latest_failure_preserves_last_successful_check(monkeypatch):
    monkeypatch.setenv("HEXWAY_URL", "https://asoc.example/api")
    monkeypatch.setenv("HEXWAY_TOKEN", "environment-only-secret")
    db = _session()
    record_test_result(
        db,
        "asoc",
        actor="operator",
        success=True,
        message="Connected · HTTP 200",
    )
    record_test_result(
        db,
        "asoc",
        actor="operator",
        success=False,
        message="TLS verification failed",
    )

    snapshot = connector_snapshot(db, "asoc")

    assert snapshot["test_status"] == "failed"
    assert snapshot["test_message"] == "TLS verification failed"
    assert snapshot["last_successful_check"]["message"] == "Connected · HTTP 200"
    assert snapshot["last_successful_check"]["at"] is not None


def test_environment_connector_route_records_success_failure_and_recovery(monkeypatch):
    monkeypatch.setenv("UI_AUTH_USERNAME", "operator")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "password")
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.env.example/api/v4")
    monkeypatch.setenv("GITLAB_PAT", "route-environment-secret")
    monkeypatch.delenv("GITLAB_INSTANCES", raising=False)
    db = _session()
    outcomes = iter(
        [
            "Connected · authentication and project discovery verified",
            requests.RequestException("network down"),
            "Connected · recovered",
        ]
    )

    def fake_test(runtime):
        assert runtime.source == "environment"
        assert runtime.get("token") == "route-environment-secret"
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(integrations_web, "test_connector_connection", fake_test)
    test_app = FastAPI()
    test_app.include_router(integrations_web.router)
    test_app.dependency_overrides[integrations_web.get_db] = lambda: db

    with TestClient(test_app) as client:
        first = client.post("/integrations/gitlab/test", follow_redirects=False)
        assert first.status_code == 303
        assert connector_snapshot(db, "gitlab")["test_status"] == "healthy"

        failed = client.post("/integrations/gitlab/test", follow_redirects=False)
        assert failed.status_code == 303
        assert "tone=error" in failed.headers["location"]
        assert connector_snapshot(db, "gitlab")["test_status"] == "failed"

        recovered = client.post("/integrations/gitlab/test", follow_redirects=False)
        assert recovered.status_code == 303
        assert "tone=success" in recovered.headers["location"]
        assert connector_snapshot(db, "gitlab")["test_status"] == "healthy"

    assert db.query(IntegrationConfig).count() == 0
    assert db.query(IntegrationConfigEvent).count() == 3
    assert "route-environment-secret" not in str(
        [
            (event.changed_fields_json, event.message)
            for event in db.query(IntegrationConfigEvent).all()
        ]
    )


def test_dedicated_credential_key_survives_ci_bearer_rotation(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "stable-integration-key")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "ci-bearer-before-rotation")
    db = _session()

    save_connector(
        db,
        "gitlab",
        {
            "api_url": "https://gitlab.example/api/v4",
            "token": "gitlab-secret",
        },
        actor="operator",
    )

    monkeypatch.setenv("PIPE_BEARER_TOKEN", "ci-bearer-after-rotation")
    runtime = load_runtime_connector("gitlab", db)

    assert runtime.get("token") == "gitlab-secret"


def _save_legacy_asoc(db, monkeypatch, *, token="legacy-asoc-secret"):
    monkeypatch.delenv("INTEGRATION_CREDENTIALS_KEY", raising=False)
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "legacy-bearer-key")
    return save_connector(
        db,
        "asoc",
        {
            "url": "https://asoc.example/api",
            "token": token,
            "space_id": "space-1",
        },
        actor="legacy-operator",
    )


def test_credential_migration_dry_run_counts_without_writes(monkeypatch):
    db = _session()
    row = _save_legacy_asoc(db, monkeypatch)
    original_ciphertext = row.secrets_encrypted
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-credential-key")

    result = migrate_integration_credentials(db, dry_run=True)

    assert result.as_dict() == {
        "total": 1,
        "empty": 0,
        "current": 0,
        "recoverable": 1,
        "migrated": 0,
        "irrecoverable": 0,
        "reset": 0,
        "remaining_irrecoverable": 0,
        "irrecoverable_connectors": [],
    }
    db.refresh(row)
    assert row.secrets_encrypted == original_ciphertext
    assert load_runtime_connector("asoc", db).error is not None


def test_credential_migration_reencrypts_atomically_and_survives_bearer_rotation(monkeypatch):
    db = _session()
    row = _save_legacy_asoc(db, monkeypatch)
    original_ciphertext = row.secrets_encrypted
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-credential-key")

    result = migrate_integration_credentials(db, dry_run=False, actor="operator")

    assert result.migrated == 1
    assert result.remaining_irrecoverable == 0
    db.refresh(row)
    assert row.secrets_encrypted != original_ciphertext
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "rotated-ci-bearer")
    assert load_runtime_connector("asoc", db).get("token") == "legacy-asoc-secret"
    event = (
        db.query(IntegrationConfigEvent)
        .filter(IntegrationConfigEvent.action == "credential_migrate")
        .one()
    )
    assert event.message == "Credentials re-encrypted with the dedicated key"
    assert "legacy-asoc-secret" not in event.changed_fields_json
    assert "legacy-asoc-secret" not in event.message


def test_credential_migration_uses_dedicated_key_before_legacy(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-credential-key")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "legacy-bearer-key")
    db = _session()
    save_connector(
        db,
        "asoc",
        {
            "url": "https://asoc.example/api",
            "token": "current-secret",
            "space_id": "space-1",
        },
        actor="operator",
    )

    result = migrate_integration_credentials(db, dry_run=False)

    assert result.current == 1
    assert result.recoverable == 0
    assert result.migrated == 0
    assert (
        db.query(IntegrationConfigEvent)
        .filter(IntegrationConfigEvent.action == "credential_migrate")
        .count()
        == 0
    )


def test_credential_migration_reports_and_explicitly_resets_corrupt_rows(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-credential-key")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "legacy-bearer-key")
    db = _session()
    row = IntegrationConfig(
        connector="asoc",
        values_json='{"url":"https://asoc.example/api"}',
        secrets_encrypted="not-valid-ciphertext",
        enabled=True,
        tested_at=datetime.now(timezone.utc),
        test_status="healthy",
        test_message="Connected · HTTP 200",
    )
    db.add(row)
    db.commit()

    blocked = migrate_integration_credentials(db, dry_run=True)

    assert blocked.irrecoverable == 1
    assert blocked.remaining_irrecoverable == 1
    assert blocked.irrecoverable_connectors == ("asoc",)
    manifest = blocked.reset_manifest_entries()
    assert set(manifest) == {"asoc"}
    assert len(manifest["asoc"]) == 64
    db.refresh(row)
    assert row.secrets_encrypted == "not-valid-ciphertext"
    assert row.enabled is True

    with pytest.raises(IntegrationConfigError, match="does not exactly match"):
        migrate_integration_credentials(
            db,
            dry_run=False,
            reset_irrecoverable=True,
            reset_manifest={"asoc": "0" * 64},
            actor="operator",
        )
    db.refresh(row)
    assert row.secrets_encrypted == "not-valid-ciphertext"
    assert row.enabled is True

    reset = migrate_integration_credentials(
        db,
        dry_run=False,
        reset_irrecoverable=True,
        reset_manifest=manifest,
        actor="operator",
    )

    assert reset.reset == 1
    assert reset.remaining_irrecoverable == 0
    db.refresh(row)
    assert row.secrets_encrypted is None
    assert row.enabled is False
    assert row.tested_at is None
    assert row.test_status is None
    assert row.test_message is None
    snapshot = connector_snapshot(db, "asoc")
    assert snapshot["health_label"] == "Disabled"
    event = (
        db.query(IntegrationConfigEvent)
        .filter(IntegrationConfigEvent.action == "credential_reset")
        .one()
    )
    assert event.message == "Irrecoverable credentials reset; connector disabled"


def test_credential_migration_requires_distinct_dedicated_and_legacy_keys(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "same-key")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "same-key")
    db = _session()

    with pytest.raises(CredentialStoreUnavailable, match="must differ"):
        migrate_integration_credentials(db)


def test_credential_migration_rolls_back_the_batch_on_commit_failure(monkeypatch):
    db = _session()
    asoc = _save_legacy_asoc(db, monkeypatch, token="asoc-secret")
    gitlab = save_connector(
        db,
        "gitlab",
        {
            "api_url": "https://gitlab.example/api/v4",
            "token": "gitlab-secret",
        },
        actor="legacy-operator",
    )
    original = {
        "asoc": asoc.secrets_encrypted,
        "gitlab": gitlab.secrets_encrypted,
    }
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-credential-key")

    def fail_commit():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        migrate_integration_credentials(db, dry_run=False)

    rows = {
        row.connector: row.secrets_encrypted
        for row in db.query(IntegrationConfig).all()
    }
    assert rows == original
    assert (
        db.query(IntegrationConfigEvent)
        .filter(IntegrationConfigEvent.action == "credential_migrate")
        .count()
        == 0
    )


def test_migrated_credentials_support_later_full_recovery_but_reject_partial_replacement(
    monkeypatch,
):
    db = _session()
    _save_legacy_asoc(db, monkeypatch)
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-key-v1")

    migration = migrate_integration_credentials(
        db,
        dry_run=False,
        actor="migration-operator",
    )

    assert migration.migrated == 1
    assert load_runtime_connector("asoc", db).get("token") == "legacy-asoc-secret"

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-key-v2")
    save_connector(
        db,
        "asoc",
        {
            "url": "https://asoc.example/api",
            "token": "replacement-secret",
            "space_id": "space-1",
        },
        actor="recovery-operator",
    )

    assert load_runtime_connector("asoc", db).get("token") == "replacement-secret"
    recovery_event = (
        db.query(IntegrationConfigEvent)
        .filter(IntegrationConfigEvent.action == "save")
        .order_by(IntegrationConfigEvent.id.desc())
        .first()
    )
    assert "credentials_replaced" in recovery_event.changed_fields_json

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-key-v3")
    with pytest.raises(CredentialStoreUnavailable, match="Enter every credential field"):
        save_connector(
            db,
            "asoc",
            {
                "url": "https://asoc.example/api",
                "token": "",
                "space_id": "space-1",
            },
            actor="partial-recovery-operator",
        )

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "dedicated-key-v2")
    assert load_runtime_connector("asoc", db).get("token") == "replacement-secret"


@pytest.mark.parametrize(
    ("connector", "initial", "replacement"),
    [
        (
            "asoc",
            {
                "url": "https://asoc.example/api",
                "token": "old-asoc",
                "space_id": "space-1",
            },
            {
                "url": "https://asoc.example/api",
                "token": "new-asoc",
                "space_id": "space-1",
            },
        ),
        (
            "ptai",
            {
                "url": "https://ptai.example",
                "token": "old-ptai",
                "verify_ssl": "true",
            },
            {
                "url": "https://ptai.example",
                "token": "new-ptai",
                "verify_ssl": "true",
            },
        ),
    ],
)
def test_explicit_credentials_replace_payload_after_key_rotation(
    monkeypatch,
    connector,
    initial,
    replacement,
):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "old-stable-key")
    db = _session()
    save_connector(db, connector, initial, actor="operator")

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "new-stable-key")
    row = save_connector(db, connector, replacement, actor="operator")

    runtime = load_runtime_connector(connector, db)
    assert runtime.get("token") == replacement["token"]
    assert runtime.error is None
    latest_event = (
        db.query(IntegrationConfigEvent)
        .order_by(IntegrationConfigEvent.id.desc())
        .first()
    )
    assert "credentials_replaced" in latest_event.changed_fields_json
    assert replacement["token"] not in row.secrets_encrypted


def test_key_rotation_requires_explicit_replacement_credentials(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "old-stable-key")
    db = _session()
    save_connector(
        db,
        "asoc",
        {
            "url": "https://asoc.example/api",
            "token": "old-asoc",
            "space_id": "space-1",
        },
        actor="operator",
    )

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "new-stable-key")
    with pytest.raises(CredentialStoreUnavailable, match="Enter every credential field"):
        save_connector(
            db,
            "asoc",
            {
                "url": "https://asoc.example/api",
                "token": "",
                "space_id": "space-1",
            },
            actor="operator",
        )


def test_structured_gitlab_credentials_can_be_replaced_after_key_rotation(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "old-stable-key")
    db = _session()
    submitted = {
        "gitlab_instances_mode": "structured",
        "gitlab_instance_name_0": "Primary",
        "gitlab_instance_api_url_0": "https://gitlab.example/api/v4",
        "gitlab_instance_token_0": "old-gitlab",
        "gitlab_instance_original_host_0": "",
    }
    save_connector(db, "gitlab", submitted, actor="operator")

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "new-stable-key")
    submitted["gitlab_instance_token_0"] = "new-gitlab"
    submitted["gitlab_instance_original_host_0"] = "gitlab.example"
    save_connector(db, "gitlab", submitted, actor="operator")

    entries = json.loads(load_runtime_connector("gitlab", db).get("instances_json"))
    assert entries[0]["token"] == "new-gitlab"


def test_disabling_ui_config_suppresses_environment_fallback(monkeypatch):
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "test-bootstrap-key-with-enough-entropy")
    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "https://mattermost.example/hooks/env")
    db = _session()

    disable_connector(db, "mattermost", actor="operator")
    runtime = load_runtime_connector("mattermost", db)

    assert runtime.source == "database"
    assert runtime.enabled is False
    assert runtime.get("webhook_url") == ""


def test_structured_gitlab_instances_support_multiple_connections_and_token_reuse(monkeypatch):
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "test-bootstrap-key-with-enough-entropy")
    db = _session()

    save_connector(
        db,
        "gitlab",
        {
            "gitlab_instances_mode": "structured",
            "gitlab_instance_name_0": "Primary",
            "gitlab_instance_api_url_0": "https://gitlab.primary.example/api/v4",
            "gitlab_instance_token_0": "primary-secret",
            "gitlab_instance_original_host_0": "",
            "gitlab_instance_name_1": "Partner",
            "gitlab_instance_api_url_1": "https://gitlab.partner.example/api/v4",
            "gitlab_instance_token_1": "partner-secret",
            "gitlab_instance_original_host_1": "",
        },
        actor="operator",
    )

    runtime = load_runtime_connector("gitlab", db)
    entries = json.loads(runtime.get("instances_json"))
    assert [entry["name"] for entry in entries] == ["Primary", "Partner"]
    assert [entry["host"] for entry in entries] == [
        "gitlab.primary.example",
        "gitlab.partner.example",
    ]

    save_connector(
        db,
        "gitlab",
        {
            "gitlab_instances_mode": "structured",
            "gitlab_instance_name_0": "Primary production",
            "gitlab_instance_api_url_0": "https://gitlab.primary.example/api/v4",
            "gitlab_instance_token_0": "",
            "gitlab_instance_original_host_0": "gitlab.primary.example",
        },
        actor="operator",
    )

    remaining = json.loads(load_runtime_connector("gitlab", db).get("instances_json"))
    assert len(remaining) == 1
    assert remaining[0]["name"] == "Primary production"
    assert remaining[0]["token"] == "primary-secret"
    snapshot = connector_snapshot(db, "gitlab")
    assert snapshot["instance_count"] == 1
    assert snapshot["instances"][0]["token_configured"] is True
    assert "primary-secret" not in str(snapshot)


def test_new_structured_gitlab_instance_requires_token(monkeypatch):
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "test-bootstrap-key-with-enough-entropy")
    db = _session()

    with pytest.raises(IntegrationConfigError, match="Access token is required"):
        save_connector(
            db,
            "gitlab",
            {
                "gitlab_instances_mode": "structured",
                "gitlab_instance_name_0": "Empty",
                "gitlab_instance_api_url_0": "https://gitlab.empty.example/api/v4",
                "gitlab_instance_token_0": "",
                "gitlab_instance_original_host_0": "",
            },
            actor="operator",
        )


def test_structured_gitlab_form_can_adopt_existing_environment_token(monkeypatch):
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "test-bootstrap-key-with-enough-entropy")
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.env.example/api/v4")
    monkeypatch.setenv("GITLAB_PAT", "environment-secret")
    db = _session()

    save_connector(
        db,
        "gitlab",
        {
            "gitlab_instances_mode": "structured",
            "gitlab_instance_name_0": "Environment GitLab",
            "gitlab_instance_api_url_0": "https://gitlab.env.example/api/v4",
            "gitlab_instance_token_0": "",
            "gitlab_instance_original_host_0": "gitlab.env.example",
        },
        actor="operator",
    )

    entries = json.loads(load_runtime_connector("gitlab", db).get("instances_json"))
    assert entries[0]["token"] == "environment-secret"


def test_structured_gitlab_instances_reject_duplicate_hosts(monkeypatch):
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "test-bootstrap-key-with-enough-entropy")
    db = _session()

    with pytest.raises(IntegrationConfigError, match="added more than once"):
        save_connector(
            db,
            "gitlab",
            {
                "gitlab_instances_mode": "structured",
                "gitlab_instance_name_0": "First",
                "gitlab_instance_api_url_0": "https://gitlab.same.example/api/v4",
                "gitlab_instance_token_0": "first-token",
                "gitlab_instance_original_host_0": "",
                "gitlab_instance_name_1": "Duplicate",
                "gitlab_instance_api_url_1": "https://gitlab.same.example/api/v4",
                "gitlab_instance_token_1": "second-token",
                "gitlab_instance_original_host_1": "",
            },
            actor="operator",
        )


def test_integrations_template_renders_manageable_fields_without_secret_values():
    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    connector = {
        "key": "asoc",
        "name": "ASOC / Vampy",
        "role": "Artifact delivery",
        "detail": "SARIF delivery.",
        "direction": "outbound",
        "configured": True,
        "enabled": True,
        "source": "database",
        "credential_ref": "database://integration-configs/asoc",
        "updated_at": None,
        "updated_by": None,
        "tested_at": "2026-07-24 09:00",
        "test_status": "failed",
        "test_message": "TLS verification failed",
        "last_successful_check": {"at": "2026-07-23 18:00", "message": "Connected"},
        "health_label": "Connected · not tested",
        "endpoints": [{"name": "ASOC API", "value": "https://asoc.example/api", "configured": True}],
        "error": None,
        "fields": [
            {"name": "url", "label": "API URL", "kind": "url", "value": "https://asoc.example/api", "configured": True},
            {"name": "token", "label": "Bearer token", "secret": True, "value": "", "configured": True},
        ],
    }
    html = env.get_template("integrations.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/integrations")),
        ui_auth_enabled=True,
        configured_count=1,
        can_manage_integrations=True,
        integration_auth_ready=True,
        credential_store_ready=True,
        focus="asoc",
        notice="",
        notice_tone="success",
        connectors=[connector],
    )

    assert "Save configuration" in html
    assert "Test connection" in html
    assert 'type="password"' in html
    assert "Configured — leave blank to keep" in html
    assert "https://asoc.example/api" in html
    assert "Close settings" in html
    assert html.index('id="integration-asoc"') < html.index('id="integration-asoc-settings"')
    assert 'aria-controls="integration-asoc-settings"' in html
    assert 'aria-expanded="true"' in html
    assert 'id="integration-asoc-settings"' in html
    assert 'tabindex="-1"' in html
    assert 'title="https://asoc.example/api"' in html
    assert 'data-copy-endpoint="https://asoc.example/api"' in html
    assert 'aria-label="Copy ASOC API endpoint"' in html
    assert "<details" not in html
    assert "Inspect the result path" not in html
    assert 'class="integration-workspace has-selection"' in html
    assert 'class="integration-settings-panel"' in html
    assert "Choose a connection" not in html
    assert 'class="integration-health is-failed"' in html
    assert "Connection failed" in html
    assert "TLS verification failed" in html
    assert "Last successful check" in html
    assert "2026-07-23 18:00" in html
    assert "Connected" in html
    assert "top-secret-token" not in html


def test_integrations_template_renders_gitlab_instances_without_json_editor():
    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    connector = {
        "key": "gitlab",
        "name": "GitLab",
        "role": "Trigger + execution source",
        "detail": "Pipeline source.",
        "direction": "inbound",
        "configured": True,
        "enabled": True,
        "source": "database",
        "credential_ref": "database://integration-configs/gitlab",
        "updated_at": None,
        "updated_by": None,
        "tested_at": None,
        "test_status": None,
        "test_message": None,
        "health_label": "Connected · not tested",
        "endpoints": [
            {"name": "Primary", "value": "https://gitlab.primary.example/api/v4", "configured": True},
            {"name": "Partner", "value": "https://gitlab.partner.example/api/v4", "configured": True},
        ],
        "error": None,
        "fields": [],
        "instance_count": 2,
        "instances": [
            {
                "id": "gl-1111111111111111",
                "name": "Primary",
                "host": "gitlab.primary.example",
                "api_url": "https://gitlab.primary.example/api/v4",
                "token_configured": True,
                "enabled": True,
                "polling_enabled": True,
                "credential_ref": "database://integration-configs/gitlab/instances/gl-1111111111111111",
                "tested_at": "2026-07-24 10:00",
                "test_status": "healthy",
                "test_message": "Connected",
                "last_successful_check": {"at": "2026-07-24 10:00", "message": "Connected"},
                "health_label": "Connected",
            },
            {
                "id": "gl-2222222222222222",
                "name": "Partner",
                "host": "gitlab.partner.example",
                "api_url": "https://gitlab.partner.example/api/v4",
                "token_configured": True,
                "enabled": True,
                "polling_enabled": False,
                "credential_ref": "database://integration-configs/gitlab/instances/gl-2222222222222222",
                "tested_at": "2026-07-24 10:05",
                "test_status": "failed",
                "test_message": "Authentication failed",
                "last_successful_check": None,
                "health_label": "Authentication failed",
            },
        ],
    }
    html = env.get_template("integrations.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/integrations")),
        ui_auth_enabled=True,
        configured_count=1,
        can_manage_integrations=True,
        integration_auth_ready=True,
        credential_store_ready=True,
        focus="gitlab",
        instance_focus="gl-2222222222222222",
        notice="",
        notice_tone="success",
        connectors=[connector],
    )

    assert "Add GitLab" in html
    assert "Primary" in html and "Partner" in html
    assert "Multiple instances (JSON)" not in html
    assert "paste JSON" not in html
    assert "<details" not in html
    assert 'action="/integrations/gitlab/instances/gl-1111111111111111/save"' in html
    assert 'action="/integrations/gitlab/instances/gl-2222222222222222/save"' in html
    assert 'action="/integrations/gitlab/instances/gl-1111111111111111/test"' in html
    assert 'action="/integrations/gitlab/instances/gl-2222222222222222/test"' in html
    assert 'action="/integrations/gitlab/instances/gl-1111111111111111/delete"' in html
    assert 'name="api_url"' in html
    assert 'name="enabled"' in html
    assert 'name="polling_enabled"' in html
    assert "Only one connection per GitLab host may poll pipelines" in html
    assert "Save this GitLab" in html
    assert "Last successful check" in html
    assert "database://integration-configs/gitlab/instances/gl-1111111111111111" in html
    assert 'id="integration-gitlab-instance-gl-2222222222222222"' in html
    assert 'class="gitlab-instance-editor is-focused"' in html
    assert 'id="integration-return-gitlab-instance-gl-2222222222222222"' in html
    assert 'data-integration-return-control="integration-return-gitlab-instance-gl-2222222222222222"' in html
    assert 'href="/integrations#integration-return-gitlab-instance-gl-2222222222222222"' in html
    assert 'id="integration-return-gitlab"' in html
    assert 'class="integration-health is-failed"' in html
    assert "Authentication failed" in html
    assert 'action="/integrations/gitlab/save"' not in html
    assert 'action="/integrations/gitlab/test"' not in html


def test_environment_gitlab_instance_requires_explicit_import_from_ui():
    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    connector = {
        "key": "gitlab",
        "name": "GitLab",
        "role": "Trigger + execution source",
        "detail": "Pipeline source.",
        "direction": "inbound",
        "configured": True,
        "enabled": True,
        "source": "environment",
        "credential_ref": "environment fallback",
        "updated_at": None,
        "updated_by": None,
        "tested_at": None,
        "test_status": None,
        "test_message": None,
        "health_label": "Connected · not tested",
        "endpoints": [],
        "error": None,
        "fields": [],
        "instances": [
            {
                "id": "gl-eeeeeeeeeeeeeeee",
                "name": "Environment GitLab",
                "host": "gitlab.env.example",
                "api_url": "https://gitlab.env.example/api/v4",
                "token_configured": True,
                "enabled": True,
                "polling_enabled": True,
                "credential_ref": "environment://gitlab/instances/gl-eeeeeeeeeeeeeeee",
                "tested_at": None,
                "test_status": None,
                "test_message": None,
                "last_successful_check": None,
                "health_label": "Connected · not tested",
            }
        ],
    }

    html = env.get_template("integrations.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/integrations")),
        configured_count=1,
        can_manage_integrations=True,
        integration_auth_ready=True,
        credential_store_ready=True,
        focus="gitlab",
        instance_focus="gl-eeeeeeeeeeeeeeee",
        notice="",
        notice_tone="success",
        connectors=[connector],
    )

    assert 'name="adopt_environment" value="true"' in html
    assert "Import & save" in html
    assert "imports the current environment credential" in html


def test_integration_catalog_renders_registry_degradation_as_failure():
    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    connector = {
        "key": "mattermost",
        "name": "Mattermost",
        "role": "Operational alerts",
        "direction": "outbound",
        "configured": True,
        "enabled": True,
        "source": "environment",
        "credential_ref": "environment fallback",
        "tested_at": None,
        "test_status": None,
        "test_message": None,
        "last_successful_check": None,
        "health_label": "Configured · not tested",
        "registry": {
            "health_status": "degraded",
            "health_reason": "Webhook endpoint is unreachable",
            "credential_ref": "environment fallback",
        },
        "endpoints": [],
        "fields": [],
        "error": None,
    }

    html = env.get_template("integrations.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/integrations")),
        configured_count=1,
        can_manage_integrations=True,
        integration_auth_ready=True,
        credential_store_ready=True,
        focus="",
        instance_focus="",
        notice="",
        notice_tone="success",
        connectors=[connector],
    )

    assert 'class="integration-health is-failed"' in html
    assert "Degraded" in html
    assert "Webhook endpoint is unreachable" in html


def test_integrations_registry_reflows_from_available_container_width():
    styles = Path("app/static/css/signal-river.css").read_text(encoding="utf-8")
    script = Path("app/static/js/integrations.js").read_text(encoding="utf-8")

    assert "container-name: integrations-page;" in styles
    assert "@container integrations-page (max-width: 1120px)" in styles
    assert "@container integrations-page (max-width: 820px)" in styles
    assert "@container integrations-page (max-width: 560px)" in styles
    assert ".integration-configure-button { min-width: 120px;" in styles
    assert ".integration-endpoint-value" in styles
    assert "grid-template-columns: minmax(0, 108px) minmax(0, 1fr);" in styles
    assert ".integration-endpoint-name {" in styles
    assert "text-overflow: ellipsis;" in styles
    assert "navigator.clipboard?.writeText" in script
    assert "window.getComputedStyle(settingsPanel).position === 'fixed'" in script
    assert "catalog.inert = true" in script
    assert "aria-modal" in script
    assert "event.key === 'Escape'" in script
    assert "scanops.integrations.focus-return" in script
    assert "window.sessionStorage.setItem" in script
    assert "window.addEventListener('pageshow', restoreTriggerFocus)" in script
    assert "window.addEventListener('hashchange', restoreTriggerFocus)" in script
    assert "document.activeElement === control" in script
    assert "data-integration-close" in script
    assert "data-integration-opener" in script
    assert "findIntegrationReturnControl" in script
    assert "data-submit-progress" in script
    assert "event.submitter" in script
    assert "event.defaultPrevented" in script
    assert "aria-busy" in script


def test_integrations_long_endpoint_label_keeps_copy_affordance_bounded():
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    long_name = "PrimaryGitLabInstanceWithoutBreaks" * 4
    long_url = "https://gitlab.example/" + ("nested/" * 30) + "api/v4"
    connector = {
        "key": "gitlab",
        "name": "GitLab",
        "role": "Trigger + execution source",
        "direction": "inbound",
        "configured": True,
        "enabled": True,
        "source": "database",
        "health_label": "Connected",
        "registry": None,
        "endpoints": [
            {
                "name": long_name,
                "value": long_url,
                "configured": True,
            }
        ],
        "fields": [],
        "error": None,
    }

    html = env.get_template("integrations.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/integrations")),
        ui_auth_enabled=True,
        configured_count=1,
        can_manage_integrations=True,
        integration_auth_ready=True,
        credential_store_ready=True,
        focus="",
        notice="",
        notice_tone="success",
        connectors=[connector],
    )

    assert f'class="integration-endpoint-name" title="{long_name}"' in html
    assert f'data-copy-endpoint="{long_url}"' in html
    assert f'aria-label="Copy {long_name} endpoint"' in html


def test_integration_redirect_closes_settings_after_success():
    response = _redirect("gitlab", "Configuration saved", keep_open=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/integrations?notice=Configuration+saved&tone=success"


def test_integration_redirect_keeps_settings_open_for_feedback():
    response = _redirect("gitlab", "Connection failed", "error")

    assert response.status_code == 303
    assert "focus=gitlab" in response.headers["location"]
    assert response.headers["location"].endswith("#integration-gitlab-settings")


class _GitLabResponse:
    def __init__(self, status_code, payload=None, *, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def _gitlab_runtime():
    return RuntimeConnector(
        key="gitlab",
        values={"api_url": "https://gitlab.example/api/v4"},
        secrets={"token": "gitlab-token"},
        enabled=True,
        source="environment",
    )


def test_gitlab_connection_test_verifies_authentication_and_project_discovery(
    monkeypatch,
):
    def fake_get(url, **_kwargs):
        if url.endswith("/version"):
            return _GitLabResponse(200, {"version": "18.0"})
        if url.endswith("/projects"):
            return _GitLabResponse(200, [{"id": 42}])
        raise AssertionError(url)

    monkeypatch.setattr("app.integration_settings.requests.get", fake_get)
    monkeypatch.setattr(
        "app.integration_settings.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Connection test must not call GitLab CI Lint")
        ),
    )

    assert (
        check_connector_connection(_gitlab_runtime())
        == "Connected · authentication and project discovery verified"
    )


@pytest.mark.parametrize(
    ("projects_response", "message"),
    [
        (_GitLabResponse(200, []), "no accessible projects"),
        (
            _GitLabResponse(200, json_error=ValueError("invalid JSON")),
            "invalid project response",
        ),
        (_GitLabResponse(200, [{}]), "project response does not contain an id"),
        (_GitLabResponse(200, ["unexpected"]), "project response does not contain an id"),
    ],
)
def test_gitlab_connection_test_rejects_unusable_project_discovery(
    monkeypatch,
    projects_response,
    message,
):
    def fake_get(url, **_kwargs):
        if url.endswith("/version"):
            return _GitLabResponse(200, {"version": "18.0"})
        if url.endswith("/projects"):
            return projects_response
        raise AssertionError(url)

    monkeypatch.setattr("app.integration_settings.requests.get", fake_get)

    with pytest.raises(IntegrationConfigError, match=message):
        check_connector_connection(_gitlab_runtime())


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "rejected the configured credentials"),
        (403, "rejected the configured credentials"),
        (500, "unavailable"),
    ],
)
def test_gitlab_connection_test_reports_project_discovery_http_failures(
    monkeypatch,
    status_code,
    message,
):
    def fake_get(url, **_kwargs):
        if url.endswith("/version"):
            return _GitLabResponse(200, {"version": "18.0"})
        if url.endswith("/projects"):
            return _GitLabResponse(status_code, {"message": "failure"})
        raise AssertionError(url)

    monkeypatch.setattr("app.integration_settings.requests.get", fake_get)

    with pytest.raises(IntegrationConfigError, match=message):
        check_connector_connection(_gitlab_runtime())


@pytest.mark.parametrize("configured_timeout", ["NaN", "+Inf", "-Inf"])
def test_connection_test_non_finite_timeout_falls_back_without_500(
    monkeypatch,
    configured_timeout,
):
    observed_timeouts = []

    def fake_get(url, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        if url.endswith("/version"):
            return _GitLabResponse(200, {"version": "18.0"})
        if url.endswith("/projects"):
            return _GitLabResponse(200, [{"id": 42}])
        raise AssertionError(url)

    monkeypatch.setenv("INTEGRATION_TEST_TIMEOUT_SECONDS", configured_timeout)
    monkeypatch.setattr("app.integration_settings.requests.get", fake_get)

    assert (
        check_connector_connection(_gitlab_runtime())
        == "Connected · authentication and project discovery verified"
    )
    assert observed_timeouts == [10.0, 10.0]
