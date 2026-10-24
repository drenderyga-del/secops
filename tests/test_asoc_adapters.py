from types import SimpleNamespace
import json
from unittest.mock import Mock
from uuid import uuid4
import pytest
import requests
from app.asoc_adapters import AsocAdapter, AsocAdapterError, validate_base_url, validate_settings
from app.integration_settings import _encrypt_secrets

SARIF = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Checkov"}}, "results": []}]}


def adapter(monkeypatch, provider="defectdojo"):
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "fixture-encryption-material")
    settings = {"defectdojo": {"engagement_id": "12", "test_title": "checkov-main"},
                "dependencytrack": {"project_id": str(uuid4())},
                "vampy": {"repository": "example/repo", "branch": "main", "vcs_url": "https://gitlab.example/repo"}}[provider]
    row = SimpleNamespace(provider=provider, api_base_url="https://asoc.example/api", enabled=True,
                          settings_json=json.dumps(settings), secrets_encrypted=_encrypt_secrets({"token": "fixture-credential"}))
    return AsocAdapter(row)


def test_defectdojo_uses_reimport_without_closing_or_reactivating_findings(monkeypatch):
    client = adapter(monkeypatch)
    request = Mock(return_value=SimpleNamespace(status_code=201, json=lambda: {"test": 123}))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    receipt = client.send("sarif", SARIF)
    args, kwargs = request.call_args
    assert args == ("POST", "https://asoc.example/api/reimport-scan/")
    assert kwargs["headers"] == {"Authorization": "Token fixture-credential"}
    assert kwargs["data"]["engagement"] == "12"
    assert kwargs["data"]["test_title"] == "checkov-main"
    assert kwargs["data"]["close_old_findings"] == "false"
    assert kwargs["data"]["do_not_reactivate"] == "true"
    assert kwargs["data"]["auto_create_context"] == "false"
    assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
    assert receipt.state == "accepted" and receipt.reference == "123"


def test_dependencytrack_sends_cyclonedx_and_requires_processing_token(monkeypatch):
    client = adapter(monkeypatch, "dependencytrack")
    processing = str(uuid4())
    request = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"token": processing}))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    assert client.send("cyclonedx", {"bomFormat": "CycloneDX", "components": []}).reference == processing
    args, kwargs = request.call_args
    assert args[1].endswith("/bom")
    assert kwargs["headers"] == {"X-Api-Key": "fixture-credential"}
    assert kwargs["data"]["project"] == client.settings["project_id"]
    assert "bom" in kwargs["files"]
    with pytest.raises(AsocAdapterError, match="does not support"):
        client.send("sarif", SARIF)


def test_vampy_uses_native_upload_shape(monkeypatch):
    client = adapter(monkeypatch, "vampy")
    request = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {}))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    assert client.send("sarif", SARIF).state == "accepted"
    args, kwargs = request.call_args
    assert args[1].endswith("/scan_uploads/")
    assert kwargs["data"]["repository"] == "example/repo"
    assert kwargs["data"]["scannerType"] == "SARIF"
    assert "scannerResultFile" in kwargs["files"]


@pytest.mark.parametrize("url", ["http://asoc.example/api", "https://user:pass@asoc.example/api", "https://asoc.example/api?key=private", "https://asoc.example/api#private", "https://asoc.example/../admin", "https://asoc.example/%2e%2e/admin"])
def test_base_url_rejects_credential_and_route_ambiguity(url):
    with pytest.raises(AsocAdapterError):
        validate_base_url(url)


@pytest.mark.parametrize("report", [ {}, {"version": "2.1.0", "runs": []},
    {"version": "2.1.0", "runs": [None]}, {"version": "2.1.0", "runs": [{"tool": None}]},
    {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": []}}}]},
])
def test_malformed_sarif_is_rejected_before_network(monkeypatch, report):
    client = adapter(monkeypatch)
    request = Mock(); monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    with pytest.raises(AsocAdapterError):
        client.send("sarif", report)
    request.assert_not_called()


def test_redirects_errors_and_transport_do_not_expose_upstream_secrets(monkeypatch):
    client = adapter(monkeypatch)
    request = Mock(return_value=SimpleNamespace(status_code=302, text="private-data"))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    with pytest.raises(AsocAdapterError, match="HTTP 302") as error:
        client.send("sarif", SARIF)
    assert "private-data" not in str(error.value)
    request.side_effect = requests.Timeout("credential-in-exception")
    with pytest.raises(AsocAdapterError) as error:
        client.send("sarif", SARIF)
    assert error.value.uncertain is True
    assert "credential-in-exception" not in str(error.value)


def test_connection_check_requires_authenticated_target_evidence(monkeypatch):
    client = adapter(monkeypatch)
    request = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"results": [{"id": 12}]}))
    monkeypatch.setattr("app.asoc_adapters.requests.request", request)
    assert client.check().state == "connected"
    request.return_value = SimpleNamespace(status_code=200, json=lambda: {"results": []})
    with pytest.raises(AsocAdapterError, match="not accessible"):
        client.check()
