"""Auth middleware: Basic for humans, Bearer for CI, /api/health open."""
import base64

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def auth_client(monkeypatch):
    monkeypatch.setenv("UI_AUTH_USERNAME", "devsec")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "s3cret")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "ci-token")
    from app.main import app

    return TestClient(app)


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_unauthenticated_request_is_rejected(auth_client):
    response = auth_client.get("/products")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_wrong_password_rejected(auth_client):
    response = auth_client.get("/products", headers=_basic("devsec", "wrong"))
    assert response.status_code == 401


def test_health_is_open_without_credentials(auth_client):
    response = auth_client.get("/api/health")
    # 200 with DB, 503 without — both prove the middleware let it through.
    assert response.status_code in (200, 503)


def test_bearer_token_accepted(auth_client):
    response = auth_client.get("/api/failed_job_filters", headers={"Authorization": "Bearer ci-token"})
    assert response.status_code != 401


def test_wrong_bearer_rejected(auth_client):
    response = auth_client.get("/api/failed_job_filters", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/ui/asoc-reporting/sync"])
def test_ci_bearer_cannot_run_operator_asoc_sync(auth_client, path):
    response = auth_client.post(
        path,
        headers={"Authorization": "Bearer ci-token", "Origin": "http://testserver"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/ui/asoc-reporting/sync"])
def test_asoc_sync_fails_closed_when_operator_auth_is_not_configured(
    auth_client, monkeypatch, path
):
    monkeypatch.delenv("UI_AUTH_USERNAME")
    monkeypatch.delenv("UI_AUTH_PASSWORD")

    response = auth_client.post(
        path,
        headers={"Authorization": "Bearer ci-token", "Origin": "http://testserver"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Operator authentication is not configured"


@pytest.mark.needs_db
def test_basic_auth_grants_ui_access(auth_client):
    response = auth_client.get("/products", headers=_basic("devsec", "s3cret"))
    assert response.status_code == 200
