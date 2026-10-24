import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.main import app
from app.models import Base, AsocDestination, AsocReportDelivery
from app.routers.api import get_db
from app.integration_settings import _decrypt_secrets


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("UI_AUTH_USERNAME", "operator")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "fixture-operator-credential")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "fixture-ci-credential")
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "fixture-encryption-material")
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    def session():
        yield db
    app.dependency_overrides[get_db] = session
    yield TestClient(app), db
    app.dependency_overrides.pop(get_db, None)
    db.close(); engine.dispose()


def operator(client, method, path, **kwargs):
    return getattr(client, method)("/api/v1/asoc" + path, auth=("operator", "fixture-operator-credential"),
                                   headers={"Origin": "http://testserver"}, **kwargs)


def create(client):
    return operator(client, "post", "/destinations", json={"name": "Example findings", "provider": "defectdojo",
        "api_base_url": "https://asoc.example/api/v2", "token": "fixture-provider-credential",
        "settings": {"engagement_id": "12", "test_title": "checkov-repo-main"}})


def report(request_id="report-1"):
    return {"request_id": request_id, "report_format": "sarif", "report": {"version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "Checkov"}}, "results": []}]}}


def send(client, destination_id, body):
    return client.post(f"/api/v1/asoc/destinations/{destination_id}/reports",
                        headers={"Authorization": "Bearer fixture-ci-credential"}, json=body)


def test_multiple_destinations_encrypt_tokens_and_require_operator(api):
    client, db = api
    first = create(client)
    second = create(client)
    assert first.status_code == 201, first.text
    assert first.json()["id"] != second.json()["id"]
    assert "fixture-provider-credential" not in first.text
    rows = db.query(AsocDestination).all()
    assert len(rows) == 2
    assert all("fixture-provider-credential" not in row.secrets_encrypted for row in rows)
    assert _decrypt_secrets(rows[0].secrets_encrypted)["token"] == "fixture-provider-credential"
    response = client.get("/api/v1/asoc/destinations", headers={"Authorization": "Bearer fixture-ci-credential"})
    assert response.status_code == 401
    assert client.post("/api/v1/asoc/destinations", json={}).status_code == 401


def test_import_is_idempotent_and_records_digest_without_report(api, monkeypatch):
    client, db = api
    destination_id = create(client).json()["id"]
    request = Mock(return_value=SimpleNamespace(status_code=201, json=lambda: {"test": 3}))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    body = report()
    response = send(client, destination_id, body)
    assert response.status_code == 202, response.text
    assert response.json()["processing_verified"] is False
    assert send(client, destination_id, body).json()["delivery_id"] == response.json()["delivery_id"]
    assert request.call_count == 1
    body["report"]["runs"][0]["results"] = [{"message": {"text": "Different finding"}}]
    assert send(client, destination_id, body).status_code == 409
    assert db.query(AsocReportDelivery).count() == 1
    delivery = db.query(AsocReportDelivery).one()
    assert delivery.state == "accepted" and len(delivery.digest_sha256) == 64
    assert not hasattr(delivery, "report")


def test_timeout_is_unknown_and_retry_cannot_send_again(api, monkeypatch):
    client, db = api
    destination_id = create(client).json()["id"]
    request = Mock(side_effect=requests.Timeout("credentials-must-not-escape"))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    response = send(client, destination_id, report())
    assert response.status_code == 502
    assert "credentials-must-not-escape" not in response.text
    assert db.query(AsocReportDelivery).one().state == "unknown"
    assert send(client, destination_id, report()).status_code == 409
    assert request.call_count == 1


def test_bad_format_and_disabled_destination_do_not_contact_provider(api, monkeypatch):
    client, db = api
    destination_id = create(client).json()["id"]
    request = Mock(); monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    wrong_format = {**report(), "report_format": "cyclonedx"}
    assert send(client, destination_id, wrong_format).status_code == 422
    row = db.get(AsocDestination, destination_id); row.enabled = False; db.commit()
    assert send(client, destination_id, report("disabled-1")).status_code == 422
    request.assert_not_called()


def test_input_validation_never_echoes_credential_values(api):
    client, db = api
    response = operator(client, "post", "/destinations", json={"name": "Example", "provider": "defectdojo",
        "api_base_url": "https://asoc.example/api", "token": "abc123", "settings": {}})
    assert response.status_code == 422
    assert "abc123" not in response.text
    assert db.query(AsocDestination).count() == 0


def test_check_does_not_send_a_report(api, monkeypatch):
    client, db = api
    destination_id = create(client).json()["id"]
    request = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"results": [{"id": 12}]}))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    response = operator(client, "post", f"/destinations/{destination_id}/check")
    assert response.status_code == 200
    assert request.call_args.args[0] == "GET"
    assert db.query(AsocReportDelivery).count() == 0


def replacement(engagement="13"):
    return {"name": "Replacement destination", "provider": "defectdojo",
            "api_base_url": "https://new-asoc.example/api/v2", "token": "different-fixture-credential",
            "settings": {"engagement_id": engagement, "test_title": "checkov-repo-main"}}


def test_replay_after_target_replacement_cannot_reuse_old_receipt(api, monkeypatch):
    client, db = api
    initial = create(client).json(); destination_id = initial["id"]
    request = Mock(return_value=SimpleNamespace(status_code=201, json=lambda: {"test": 3}))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    accepted = send(client, destination_id, report())
    assert accepted.status_code == 202
    changed = operator(client, "put", f"/destinations/{destination_id}", json=replacement())
    assert changed.status_code == 200
    assert changed.json()["config_version"] != initial["config_version"]
    assert send(client, destination_id, report()).status_code == 409
    assert request.call_count == 1
    delivery = db.query(AsocReportDelivery).one()
    assert delivery.config_version == initial["config_version"]
    snapshot = json.loads(delivery.target_json)
    assert snapshot["api_base_url"] == "https://asoc.example/api/v2"
    assert snapshot["settings"]["engagement_id"] == "12"
    assert "credential" not in delivery.target_json


def test_destination_edit_during_ambiguous_send_retains_original_target(api, monkeypatch):
    client, db = api
    initial = create(client).json(); destination_id = initial["id"]
    def interrupted_request(method, url, **kwargs):
        assert url == "https://asoc.example/api/v2/reimport-scan/"
        changed = operator(client, "put", f"/destinations/{destination_id}", json=replacement())
        assert changed.status_code == 200
        raise requests.Timeout("upstream outcome is unknown")
    monkeypatch.setattr("app.asoc_adapters.requests.request", interrupted_request)
    assert send(client, destination_id, report()).status_code == 502
    delivery = db.query(AsocReportDelivery).one()
    assert delivery.state == "unknown"
    assert delivery.config_version == initial["config_version"]
    assert json.loads(delivery.target_json)["settings"]["engagement_id"] == "12"
    journal = operator(client, "get", "/deliveries").json()["deliveries"][0]
    assert journal["target"]["api_base_url"] == "https://asoc.example/api/v2"
    assert send(client, destination_id, report()).status_code == 409


def test_mutating_5xx_is_unknown_and_requires_inspection_before_retry(api, monkeypatch):
    client, db = api
    destination_id = create(client).json()["id"]
    request = Mock(return_value=SimpleNamespace(status_code=500))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    assert send(client, destination_id, report()).status_code == 502
    assert db.query(AsocReportDelivery).one().state == "unknown"
    replay = send(client, destination_id, report())
    assert replay.status_code == 409
    assert "Inspect" in replay.text
    assert request.call_count == 1


def test_new_destination_storage_never_falls_back_to_ci_key(api, monkeypatch):
    client, db = api
    destination_id = create(client).json()["id"]
    monkeypatch.delenv("INTEGRATION_CREDENTIALS_KEY")
    assert create(client).status_code == 503
    assert send(client, destination_id, report()).status_code == 503
    assert operator(client, "post", f"/destinations/{destination_id}/check").status_code == 503
    assert db.query(AsocDestination).count() == 1
    assert db.query(AsocReportDelivery).count() == 0
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "fixture-ci-credential")
    assert create(client).status_code == 503


@pytest.mark.parametrize('field,value', [
    ('PIPE_BEARER_TOKEN', 'fixture-ci-credential'),
    ('UI_AUTH_PASSWORD', 'fixture-operator-credential'),
    ('INTEGRATION_CREDENTIALS_KEY', 'fixture-encryption-material'),
    ('SCANOPS_AGENT_API_TOKEN', 'fixture-agent-' + 'a' * 40),
])
def test_provider_token_cannot_reuse_application_credentials(api, monkeypatch, field, value):
    client, db = api
    monkeypatch.setenv(field, value)
    request = Mock(); monkeypatch.setattr('app.asoc_adapters.requests.request', request)
    body = replacement(); body['token'] = value
    response = operator(client, 'post', '/destinations', json=body)
    assert response.status_code == 422, response.text
    assert value not in response.text
    assert db.query(AsocDestination).count() == 0
    request.assert_not_called()


def test_provider_base_url_never_persists_credential_paths(api, monkeypatch):
    client, db = api
    request = Mock(); monkeypatch.setattr('app.asoc_adapters.requests.request', request)
    body = replacement(); body['api_base_url'] += '/' + 'glpat-' + 'a' * 20
    response = operator(client, 'post', '/destinations', json=body)
    assert response.status_code == 422
    assert body['api_base_url'] not in response.text
    assert db.query(AsocDestination).count() == 0
    request.assert_not_called()
