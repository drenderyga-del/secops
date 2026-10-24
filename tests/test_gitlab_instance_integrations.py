import json
import os
import threading

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.connector_registry import refresh_registry
from app.integration_settings import (
    IntegrationConfigError,
    RuntimeConnector,
    connector_snapshot,
    create_gitlab_instance,
    delete_gitlab_instance,
    disable_connector,
    load_runtime_connector,
    record_test_result,
    runtime_setting,
    save_connector,
    save_gitlab_instance,
    test_gitlab_instance_connection as check_gitlab_instance_connection,
)
from app.models import (
    Base,
    IntegrationConfig,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
)
from app.routers.web import integrations as integrations_web


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


def _full_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _save_two(db, monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "instance-test-key")
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
    return connector_snapshot(db, "gitlab")["instances"]


def test_instance_snapshot_has_stable_public_ids_and_no_secrets(monkeypatch):
    db = _session()
    first = _save_two(db, monkeypatch)
    second = connector_snapshot(db, "gitlab")["instances"]

    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert len({item["credential_ref"] for item in first}) == 2
    assert all(item["credential_ref"].endswith(item["id"]) for item in first)
    assert "primary-secret" not in str(first)
    assert "partner-secret" not in str(first)


def test_instance_test_targets_only_selected_gitlab(monkeypatch):
    db = _session()
    instances = _save_two(db, monkeypatch)
    selected = instances[0]
    seen = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        seen.append((url, kwargs["headers"]["PRIVATE-TOKEN"], kwargs["allow_redirects"]))
        if url.endswith("/version"):
            return Response({"version": "18"})
        return Response([{"id": 42}])

    monkeypatch.setattr("app.integration_settings.requests.get", fake_get)
    monkeypatch.setattr(
        "app.integration_settings.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Connection test must not call GitLab CI Lint")
        ),
    )

    message = check_gitlab_instance_connection(
        load_runtime_connector("gitlab", db), selected["id"]
    )

    assert message == "Connected · authentication and project discovery verified"
    assert len(seen) == 2
    assert all(item[1] == "primary-secret" for item in seen)
    assert all(item[2] is False for item in seen)
    assert all("gitlab.partner.example" not in item[0] for item in seen)


def test_instance_health_is_independent_and_keeps_last_success(monkeypatch):
    db = _session()
    first, second = _save_two(db, monkeypatch)

    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Primary healthy",
        instance_id=first["id"],
    )
    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=False,
        message="Primary timeout",
        instance_id=first["id"],
    )
    snapshot = connector_snapshot(db, "gitlab")
    first_state, second_state = snapshot["instances"]

    assert first_state["test_status"] == "failed"
    assert first_state["test_message"] == "Primary timeout"
    assert first_state["last_successful_check"]["message"] == "Primary healthy"
    assert second_state["test_status"] is None
    assert second_state["last_successful_check"] is None


def test_instance_save_does_not_change_other_instance_or_polling(monkeypatch):
    db = _session()
    first, second = _save_two(db, monkeypatch)
    before = json.loads(
        load_runtime_connector("gitlab", db).get("instances_json")
    )
    before[1]["polling"] = {"enabled": False, "interval_seconds": 45}
    row = db.query(IntegrationConfig).filter_by(connector="gitlab").one()
    runtime = load_runtime_connector("gitlab", db)
    runtime_entries = json.loads(runtime.get("instances_json"))
    runtime_entries[1]["polling"] = before[1]["polling"]
    from app.integration_settings import _encrypt_secrets

    secrets = dict(runtime.secrets)
    secrets["instances_json"] = json.dumps(runtime_entries)
    row.secrets_encrypted = _encrypt_secrets(secrets)
    db.commit()

    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": "Primary renamed",
            "api_url": "https://gitlab.primary.example/api/v4",
            "token": "primary-new-secret",
        },
        actor="operator",
    )

    after = json.loads(load_runtime_connector("gitlab", db).get("instances_json"))
    assert after[0]["name"] == "Primary renamed"
    assert after[0]["token"] == "primary-new-secret"
    assert after[1]["id"] == second["id"]
    assert after[1]["token"] == "partner-secret"
    assert after[1]["polling"] == {"enabled": False, "interval_seconds": 45}


def test_instance_connection_change_invalidates_current_check_and_keeps_last_success(
    monkeypatch,
):
    db = _session()
    first, _second = _save_two(db, monkeypatch)
    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Connected old endpoint",
        instance_id=first["id"],
    )

    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": first["name"],
            "api_url": "https://gitlab.replacement.example/api/v4",
            "token": "replacement-secret",
        },
        actor="operator",
    )

    updated = next(
        item
        for item in connector_snapshot(db, "gitlab")["instances"]
        if item["id"] == first["id"]
    )
    assert updated["api_url"] == "https://gitlab.replacement.example/api/v4"
    assert updated["tested_at"] is None
    assert updated["test_status"] is None
    assert updated["test_message"] is None
    assert updated["last_successful_check"]["message"] == "Connected old endpoint"

    state = (
        db.query(IntegrationInstanceTestState)
        .filter_by(connector="gitlab", instance_id=first["id"])
        .one()
    )
    assert state.test_status == "unknown"
    assert state.test_message == "Configuration changed · test required"
    assert state.last_success_message == "Connected old endpoint"

    event = (
        db.query(IntegrationConfigEvent)
        .filter_by(connector="gitlab", action="save")
        .order_by(IntegrationConfigEvent.id.desc())
        .first()
    )
    assert json.loads(event.changed_fields_json) == {
        "fields": ["api_url", "token"],
        "instance_id": first["id"],
    }


def test_stale_connection_test_result_cannot_overwrite_changed_configuration(
    monkeypatch,
):
    db = _session()
    first, _second = _save_two(db, monkeypatch)
    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Connected original configuration",
        instance_id=first["id"],
    )
    runtime_before_save = load_runtime_connector("gitlab", db)
    monkeypatch.setattr(
        "app.integration_settings._test_gitlab_connection",
        lambda *_args, **_kwargs: "Connected result from in-flight test",
    )
    in_flight_result = check_gitlab_instance_connection(
        runtime_before_save,
        first["id"],
    )

    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": first["name"],
            "api_url": "https://gitlab.changed.example/api/v4",
            "token": "changed-secret",
        },
        actor="operator",
    )

    with pytest.raises(IntegrationConfigError, match="configuration changed"):
        record_test_result(
            db,
            "gitlab",
            actor="operator",
            success=True,
            message=in_flight_result,
            instance_id=first["id"],
        )
    db.rollback()

    updated = next(
        item
        for item in connector_snapshot(db, "gitlab")["instances"]
        if item["id"] == first["id"]
    )
    assert updated["test_status"] is None
    assert updated["test_message"] is None
    assert (
        updated["last_successful_check"]["message"]
        == "Connected original configuration"
    )
    assert (
        db.query(IntegrationConfigEvent)
        .filter_by(
            connector="gitlab",
            action="test",
            message="Connected result from in-flight test",
        )
        .count()
        == 0
    )


def test_stale_failed_connection_test_result_uses_the_same_generation_guard(
    monkeypatch,
):
    db = _session()
    first, _second = _save_two(db, monkeypatch)
    runtime_before_save = load_runtime_connector("gitlab", db)

    def fail_test(*_args, **_kwargs):
        raise requests.Timeout("slow old endpoint")

    monkeypatch.setattr(
        "app.integration_settings._test_gitlab_connection",
        fail_test,
    )
    with pytest.raises(IntegrationConfigError) as test_error:
        check_gitlab_instance_connection(runtime_before_save, first["id"])
    in_flight_failure = str(test_error.value)
    assert "timed out" in in_flight_failure

    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": first["name"],
            "api_url": "https://gitlab.changed.example/api/v4",
            "token": "changed-secret",
        },
        actor="operator",
    )

    with pytest.raises(IntegrationConfigError, match="configuration changed"):
        record_test_result(
            db,
            "gitlab",
            actor="operator",
            success=False,
            message=in_flight_failure,
            instance_id=first["id"],
        )


def test_invalidated_instance_health_is_unknown_in_connector_registry(monkeypatch):
    db = _full_session()
    first, _second = _save_two(db, monkeypatch)
    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Connected original configuration",
        instance_id=first["id"],
    )
    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": first["name"],
            "api_url": "https://gitlab.changed.example/api/v4",
            "token": "changed-secret",
        },
        actor="operator",
    )

    registry = refresh_registry(db)
    instance = next(
        item
        for item in registry["instances"]
        if item["instance_key"] == f"gitlab:{first['id']}"
    )
    assert instance["health_status"] == "unknown"
    assert instance["health_reason"] == "Configuration changed · test required"


def test_instance_name_and_polling_changes_retain_health_and_audit_actual_fields(
    monkeypatch,
):
    db = _session()
    first, _second = _save_two(db, monkeypatch)
    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Connected · authentication and project discovery verified",
        instance_id=first["id"],
    )

    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": "Primary renamed",
            "api_url": first["api_url"],
            "token": "",
        },
        actor="operator",
    )
    renamed = next(
        item
        for item in connector_snapshot(db, "gitlab")["instances"]
        if item["id"] == first["id"]
    )
    assert renamed["test_status"] == "healthy"
    assert (
        renamed["test_message"]
        == "Connected · authentication and project discovery verified"
    )
    rename_event = (
        db.query(IntegrationConfigEvent)
        .filter_by(connector="gitlab", action="save")
        .order_by(IntegrationConfigEvent.id.desc())
        .first()
    )
    assert json.loads(rename_event.changed_fields_json) == {
        "fields": ["name"],
        "instance_id": first["id"],
    }

    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": "Primary renamed",
            "api_url": first["api_url"],
            "token": "",
            "polling_enabled": "false",
        },
        actor="operator",
    )
    polling_disabled = next(
        item
        for item in connector_snapshot(db, "gitlab")["instances"]
        if item["id"] == first["id"]
    )
    assert polling_disabled["polling_enabled"] is False
    assert polling_disabled["test_status"] == "healthy"
    polling_event = (
        db.query(IntegrationConfigEvent)
        .filter_by(connector="gitlab", action="save")
        .order_by(IntegrationConfigEvent.id.desc())
        .first()
    )
    assert json.loads(polling_event.changed_fields_json) == {
        "fields": ["polling_enabled"],
        "instance_id": first["id"],
    }


def test_environment_instance_is_visible_but_edit_is_explicitly_read_only(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "instance-test-key")
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.env.example/api/v4")
    monkeypatch.delenv("GITLAB_PAT", raising=False)
    monkeypatch.delenv("GITLAB", raising=False)
    monkeypatch.delenv("GITLAB_INSTANCES", raising=False)
    db = _session()

    snapshot = connector_snapshot(db, "gitlab")
    assert len(snapshot["instances"]) == 1
    instance = snapshot["instances"][0]
    assert instance["token_configured"] is False
    assert instance["credential_ref"].startswith("environment://")

    with pytest.raises(IntegrationConfigError, match="read-only"):
        save_gitlab_instance(
            db,
            instance["id"],
            {"name": "Env", "api_url": instance["api_url"], "token": "new"},
            actor="operator",
        )


def test_environment_instance_requires_explicit_import_before_managed_save(
    monkeypatch,
):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "instance-test-key")
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.env.example/api/v4")
    monkeypatch.setenv("GITLAB_PAT", "environment-secret")
    monkeypatch.delenv("GITLAB_INSTANCES", raising=False)
    db = _session()
    instance = connector_snapshot(db, "gitlab")["instances"][0]

    with pytest.raises(IntegrationConfigError, match="Confirm environment import"):
        save_gitlab_instance(
            db,
            instance["id"],
            {
                "name": "Environment",
                "api_url": instance["api_url"],
                "token": "",
            },
            actor="operator",
        )
    assert db.query(IntegrationConfig).count() == 0

    save_gitlab_instance(
        db,
        instance["id"],
        {
            "name": "Managed environment",
            "api_url": instance["api_url"],
            "token": "",
            "adopt_environment": "true",
        },
        actor="operator",
    )

    runtime = load_runtime_connector("gitlab", db)
    assert runtime.source == "database"
    assert runtime.get("token") == "environment-secret"
    assert json.loads(runtime.get("instances_json"))[0]["name"] == "Managed environment"


def test_same_host_instances_require_unambiguous_polling_credential(monkeypatch):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "instance-test-key")
    db = _session()
    _row, first_id = create_gitlab_instance(
        db,
        {
            "name": "Runtime",
            "api_url": "https://gitlab.same.example/api/v4",
            "token": "runtime-secret",
        },
        actor="operator",
    )
    _row, second_id = create_gitlab_instance(
        db,
        {
            "name": "Test only",
            "api_url": "https://gitlab.same.example/api/v4",
            "token": "test-secret",
            "polling_enabled": "false",
        },
        actor="operator",
    )

    assert first_id != second_id
    instances = connector_snapshot(db, "gitlab")["instances"]
    assert [item["polling_enabled"] for item in instances] == [True, False]


@pytest.mark.parametrize(
    ("instance_enabled", "polling_enabled"),
    [
        ("true", "false"),
        ("false", "true"),
        ("false", "false"),
    ],
)
def test_no_active_gitlab_polling_instance_exports_no_runtime_credentials(
    monkeypatch,
    instance_enabled,
    polling_enabled,
):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "instance-test-key")
    monkeypatch.setenv("PIPELINE_POLLER_ENABLED", "true")
    monkeypatch.setenv("CONFIG_SCAN_ENABLED", "true")
    db = _session()
    create_gitlab_instance(
        db,
        {
            "name": "Test only",
            "api_url": "https://gitlab.test-only.example/api/v4",
            "token": "test-only-secret",
            "enabled": instance_enabled,
            "polling_enabled": polling_enabled,
        },
        actor="operator",
    )
    monkeypatch.setattr(
        "app.integration_settings.load_runtime_connector",
        lambda key, _db=None: load_runtime_connector(key, db),
    )

    assert runtime_setting("GITLAB_INSTANCES", "missing") == "[]"
    assert runtime_setting("GITLAB_PAT", "missing") == ""
    assert runtime_setting("GITLAB_API_URL", "missing") == ""

    from app.config_scan_poller import ConfigScanPoller
    from app.pipeline_tracker import PipelinePoller

    assert PipelinePoller().is_enabled() is False
    assert ConfigScanPoller().is_enabled() is False


def test_legacy_environment_gitlab_instance_mapping_remains_pollable(monkeypatch):
    monkeypatch.setenv(
        "GITLAB_INSTANCES",
        json.dumps(
            {
                "gitlab.legacy.example": {
                    "api_url": "https://gitlab.legacy.example/api/v4",
                    "token": "legacy-secret",
                }
            }
        ),
    )
    monkeypatch.delenv("GITLAB_PAT", raising=False)
    monkeypatch.delenv("GITLAB", raising=False)
    db = _session()
    monkeypatch.setattr(
        "app.integration_settings.load_runtime_connector",
        lambda key, _db=None: load_runtime_connector(key, db),
    )

    exported = json.loads(runtime_setting("GITLAB_INSTANCES", ""))
    assert len(exported) == 1
    assert exported[0]["host"] == "gitlab.legacy.example"
    assert exported[0]["api_url"] == "https://gitlab.legacy.example/api/v4"
    assert runtime_setting("GITLAB_PAT", "") == "legacy-secret"
    assert (
        runtime_setting("GITLAB_API_URL", "")
        == "https://gitlab.legacy.example/api/v4"
    )

    from app.config_scan_poller import ConfigScanPoller
    from app.pipeline_tracker import PipelinePoller

    assert PipelinePoller().is_enabled() is True
    assert ConfigScanPoller().is_enabled() is True


def test_empty_environment_instance_list_falls_back_to_direct_credentials(
    monkeypatch,
):
    monkeypatch.setenv("GITLAB_INSTANCES", "[]")
    monkeypatch.setenv(
        "GITLAB_API_URL", "https://gitlab.direct.example/api/v4"
    )
    monkeypatch.setenv("GITLAB_PAT", "direct-secret")
    db = _session()
    monkeypatch.setattr(
        "app.integration_settings.load_runtime_connector",
        lambda key, _db=None: load_runtime_connector(key, db),
    )

    exported = json.loads(runtime_setting("GITLAB_INSTANCES", ""))
    assert len(exported) == 1
    assert exported[0]["host"] == "gitlab.direct.example"
    assert exported[0]["token"] == "direct-secret"
    assert runtime_setting("GITLAB_PAT", "") == "direct-secret"
    assert (
        runtime_setting("GITLAB_API_URL", "")
        == "https://gitlab.direct.example/api/v4"
    )

    from app.config_scan_poller import ConfigScanPoller
    from app.pipeline_tracker import PipelinePoller

    assert PipelinePoller().is_enabled() is True
    assert ConfigScanPoller().is_enabled() is True


def test_explicit_empty_environment_instances_allow_first_managed_create(
    monkeypatch,
):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "instance-test-key")
    monkeypatch.setenv("GITLAB_INSTANCES", "[]")
    monkeypatch.delenv("GITLAB_PAT", raising=False)
    monkeypatch.delenv("GITLAB", raising=False)
    db = _session()

    _row, instance_id = create_gitlab_instance(
        db,
        {
            "name": "First managed GitLab",
            "api_url": "https://gitlab.first.example/api/v4",
            "token": "first-secret",
        },
        actor="operator",
    )

    snapshot = connector_snapshot(db, "gitlab")
    assert snapshot["source"] == "database"
    assert [instance["id"] for instance in snapshot["instances"]] == [instance_id]
    assert snapshot["instances"][0]["name"] == "First managed GitLab"
    assert "first-secret" not in str(snapshot)


def test_delete_instance_removes_only_its_health_state(monkeypatch):
    db = _session()
    first, second = _save_two(db, monkeypatch)
    for instance in (first, second):
        record_test_result(
            db,
            "gitlab",
            actor="operator",
            success=True,
            message=f"{instance['name']} healthy",
            instance_id=instance["id"],
        )

    delete_gitlab_instance(db, first["id"], actor="operator")

    assert [item["id"] for item in connector_snapshot(db, "gitlab")["instances"]] == [
        second["id"]
    ]
    states = db.query(IntegrationInstanceTestState).all()
    assert [state.instance_id for state in states] == [second["id"]]


def test_mismatched_instance_test_route_is_404_without_state(monkeypatch):
    monkeypatch.setenv("UI_AUTH_USERNAME", "operator")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "password")
    db = _session()
    _save_two(db, monkeypatch)
    app = FastAPI()
    app.include_router(integrations_web.router)
    app.dependency_overrides[integrations_web.get_db] = lambda: db

    with TestClient(app) as client:
        response = client.post(
            "/integrations/gitlab/instances/gl-0000000000000000/test",
            follow_redirects=False,
        )

    assert response.status_code == 404
    assert db.query(IntegrationInstanceTestState).count() == 0
    assert (
        db.query(IntegrationConfigEvent)
        .filter(IntegrationConfigEvent.action == "test")
        .count()
        == 0
    )


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            requests.Timeout("slow"),
            "timed out",
        ),
        (
            requests.exceptions.SSLError("unknown ca"),
            "TLS verification failed",
        ),
        (
            IntegrationConfigError("GitLab rejected the configured credentials (HTTP 403)"),
            "rejected the configured credentials",
        ),
    ],
)
def test_instance_route_records_actionable_failure_without_touching_peer(
    monkeypatch,
    failure,
    expected,
):
    monkeypatch.setenv("UI_AUTH_USERNAME", "operator")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "password")
    db = _session()
    first, second = _save_two(db, monkeypatch)
    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Primary healthy",
        instance_id=first["id"],
    )
    monkeypatch.setattr(
        integrations_web,
        "test_gitlab_instance_connection",
        lambda *_args: (_ for _ in ()).throw(failure),
    )
    app = FastAPI()
    app.include_router(integrations_web.router)
    app.dependency_overrides[integrations_web.get_db] = lambda: db

    with TestClient(app) as client:
        response = client.post(
            f"/integrations/gitlab/instances/{first['id']}/test",
            follow_redirects=False,
        )

    assert response.status_code == 303
    snapshot = connector_snapshot(db, "gitlab")
    first_state, second_state = snapshot["instances"]
    assert first_state["test_status"] == "failed"
    assert expected in first_state["test_message"]
    assert first_state["last_successful_check"]["message"] == "Primary healthy"
    assert second_state["test_status"] is None
    assert second_state["id"] == second["id"]


def test_disabled_instance_cannot_be_tested(monkeypatch):
    db = _session()
    first, _second = _save_two(db, monkeypatch)
    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Connected · authentication and project discovery verified",
        instance_id=first["id"],
    )
    save_gitlab_instance(
        db,
        first["id"],
        {
            "name": first["name"],
            "api_url": first["api_url"],
            "token": "",
            "enabled": "false",
        },
        actor="operator",
    )

    disabled = next(
        instance
        for instance in connector_snapshot(db, "gitlab")["instances"]
        if instance["id"] == first["id"]
    )
    assert disabled["health_label"] == "Disabled"
    assert (
        disabled["test_message"]
        == "Connected · authentication and project discovery verified"
    )
    assert (
        disabled["last_successful_check"]["message"]
        == "Connected · authentication and project discovery verified"
    )

    with pytest.raises(IntegrationConfigError, match="disabled"):
        check_gitlab_instance_connection(
            load_runtime_connector("gitlab", db), first["id"]
        )


def test_connector_disable_preserves_safe_instance_metadata_and_recovery(
    monkeypatch,
):
    db = _session()
    before = _save_two(db, monkeypatch)
    disable_connector(db, "gitlab", actor="operator")

    disabled = connector_snapshot(db, "gitlab")
    assert disabled["enabled"] is False
    assert [item["id"] for item in disabled["instances"]] == [
        item["id"] for item in before
    ]
    assert all(item["token_configured"] for item in disabled["instances"])
    assert "primary-secret" not in str(disabled)
    monkeypatch.setattr(
        "app.integration_settings.load_runtime_connector",
        lambda key, _db=None: load_runtime_connector(key, db),
    )
    assert runtime_setting("GITLAB_INSTANCES", "") == ""
    assert runtime_setting("GITLAB_PAT", "missing") == ""
    assert runtime_setting("GITLAB_API_URL", "missing") == ""

    save_connector(
        db,
        "gitlab",
        {
            "gitlab_instances_mode": "structured",
            **{
                key: value
                for index, item in enumerate(disabled["instances"])
                for key, value in {
                    f"gitlab_instance_id_{index}": item["id"],
                    f"gitlab_instance_name_{index}": item["name"],
                    f"gitlab_instance_api_url_{index}": item["api_url"],
                    f"gitlab_instance_token_{index}": "",
                    f"gitlab_instance_original_host_{index}": item["host"],
                    f"gitlab_instance_enabled_{index}": "true",
                    f"gitlab_instance_polling_enabled_{index}": "true",
                }.items()
            },
        },
        actor="operator",
    )

    recovered = connector_snapshot(db, "gitlab")
    assert recovered["enabled"] is True
    assert [item["id"] for item in recovered["instances"]] == [
        item["id"] for item in before
    ]
    private = json.loads(
        load_runtime_connector("gitlab", db).get("instances_json")
    )
    assert [item["token"] for item in private] == [
        "primary-secret",
        "partner-secret",
    ]


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="requires TEST_POSTGRES_URL for real row-lock semantics",
)
def test_concurrent_instance_saves_preserve_both_changes(monkeypatch):
    """A/B start together; PostgreSQL row lock makes the second re-read A."""
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    seed = factory()
    instances = _save_two(seed, monkeypatch)
    seed.close()
    barrier = threading.Barrier(2)
    errors = []

    def update(instance, name):
        session = factory()
        try:
            barrier.wait(timeout=5)
            save_gitlab_instance(
                session,
                instance["id"],
                {
                    "name": name,
                    "api_url": instance["api_url"],
                    "token": "",
                },
                actor=name,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=update, args=(instances[0], "Primary concurrent")),
        threading.Thread(target=update, args=(instances[1], "Partner concurrent")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    verify = factory()
    try:
        names = [
            item["name"]
            for item in connector_snapshot(verify, "gitlab")["instances"]
        ]
        assert names == ["Primary concurrent", "Partner concurrent"]
    finally:
        verify.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
