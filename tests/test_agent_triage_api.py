"""The dedicated agent credential must never become an operator or CI token."""
from datetime import datetime, timezone
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.main import app
from app.models import Base, AgentJobContext, AgentTriageProposal, JobReport, JobReportDetail, JobStatus, Product, Repository
from app.routers.api import get_db


@pytest.fixture
def api(monkeypatch):
    agent_token = "local-agent-" + "a" * 40
    monkeypatch.setenv("SCANOPS_AGENT_API_TOKEN", agent_token)
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "local-ci-credential")
    monkeypatch.setenv("UI_AUTH_USERNAME", "operator")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "local-operator-credential")
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "local-encryption-credential")
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    product = Product(name="Example", path="example", gitlab_base_url="https://gitlab.example")
    db.add(product); db.flush()
    repo = Repository(product_id=product.id, path="example/repo")
    db.add(repo); db.flush()
    job = JobReport(repository_id=repo.id, job_name="semgrep_scan", job_status=JobStatus.FAILED,
                    finished_at=datetime.now(timezone.utc), ref="main")
    db.add(job); db.flush()
    db.add(JobReportDetail(job_report_id=job.id, raw_payload={"trace": "NEVER SHARE RAW LOGS", "token": "private-data"}))
    db.commit()
    def session():
        yield db
    app.dependency_overrides[get_db] = session
    client = TestClient(app)
    yield client, db, job.id, {"Authorization": "Bearer " + agent_token}
    app.dependency_overrides.pop(get_db, None)
    db.close(); engine.dispose()


def operator_request(client, method, path, **kwargs):
    return getattr(client, method)(path, auth=("operator", "local-operator-credential"),
                                   headers={"Origin": "http://testserver"}, **kwargs)


def share(client, job_id, diagnostics="Scanner failed because its rules file is absent."):
    return operator_request(client, "put", f"/api/v1/agent-operations/jobs/{job_id}/context", json={"diagnostics": diagnostics})


def proposal(version, request_id="diagnosis-1"):
    return {"request_id": request_id, "context_version": version, "classification": "configuration",
            "summary": "Rules file is missing", "rationale": "The approved context says the file is absent.",
            "suggested_fix": "Restore the rules file and rerun the scanner.",
            "patch": "--- a/.semgrep.yml\n+++ b/.semgrep.yml\n@@ -1 +1 @@\n-old\n+new\n"}


def test_agent_is_scoped_and_ci_cannot_read_context(api, monkeypatch):
    client, db, job_id, agent = api
    assert client.get("/api/v1/agent/jobs", headers=agent).json()["jobs"] == []
    assert client.get(f"/api/v1/agent/jobs/{job_id}", headers=agent).status_code == 404
    for path in ("/api/v1/agent/jobs", f"/api/v1/agent/jobs/{job_id}"):
        assert client.get(path, headers={"Authorization": "Bearer local-ci-credential"}).status_code == 401
        assert client.get(path, auth=("operator", "local-operator-credential")).status_code == 401
    assert client.get("/api/registry", headers=agent).status_code == 401
    assert client.get("/api/v1/asoc/destinations", headers=agent).status_code == 401
    monkeypatch.delenv("SCANOPS_AGENT_API_TOKEN")
    assert client.get("/api/v1/agent/jobs", headers=agent).status_code == 401


def test_context_is_explicit_and_does_not_include_raw_payload(api):
    client, db, job_id, agent = api
    response = share(client, job_id)
    assert response.status_code == 200, response.text
    result = client.get(f"/api/v1/agent/jobs/{job_id}", headers=agent)
    assert result.status_code == 200
    assert "NEVER SHARE" not in result.text
    assert "private-data" not in result.text
    assert "raw_payload" not in result.text
    assert len(result.json()["context_version"]) == 64
    assert client.get("/api/v1/agent/jobs", headers=agent).json()["jobs"][0]["job_id"] == job_id


def test_operator_sharing_requires_auth_csrf_and_rejects_secrets(api, monkeypatch):
    client, db, job_id, agent = api
    path = f"/api/v1/agent-operations/jobs/{job_id}/context"
    assert client.put(path, headers=agent, json={"diagnostics": "hello"}).status_code == 401
    assert client.put(path, auth=("operator", "local-operator-credential"), json={"diagnostics": "hello"}).status_code == 403
    assert share(client, job_id, "password=must-not-share").status_code == 422
    assert share(client, job_id, "found https://user:pass@gitlab.example/repo").status_code == 422
    monkeypatch.delenv("UI_AUTH_PASSWORD")
    assert operator_request(client, "put", path, json={"diagnostics": "hello"}).status_code == 503
    assert db.get(AgentJobContext, job_id) is None


def test_proposal_is_idempotent_advisory_and_operator_reviewed(api):
    client, db, job_id, agent = api
    version = share(client, job_id).json()["context_version"]
    payload = proposal(version)
    path = f"/api/v1/agent/jobs/{job_id}/proposals"
    response = client.post(path, headers=agent, json=payload)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["state"] == "pending" and row["applied"] is False
    replay = client.post(path, headers=agent, json=payload)
    assert replay.json()["id"] == row["id"]
    assert db.query(AgentTriageProposal).count() == 1
    changed = {**payload, "summary": "A different diagnosis"}
    assert client.post(path, headers=agent, json=changed).status_code == 409
    review_path = f'/api/v1/agent-operations/proposals/{row["id"]}/review'
    assert client.post(review_path, headers=agent, json={"decision": "accepted"}).status_code == 401
    accepted = operator_request(client, "post", review_path, json={"decision": "accepted"})
    assert accepted.status_code == 200
    assert accepted.json()["reviewed_by"] == "operator"
    assert accepted.json()["applied"] is False
    assert operator_request(client, "post", review_path, json={"decision": "rejected"}).status_code == 409


def test_context_changes_and_revocation_invalidate_access_and_approval(api):
    client, db, job_id, agent = api
    version = share(client, job_id).json()["context_version"]
    row = client.post(f"/api/v1/agent/jobs/{job_id}/proposals", headers=agent, json=proposal(version)).json()
    share(client, job_id, "Updated diagnostic evidence.")
    assert client.post(f"/api/v1/agent/jobs/{job_id}/proposals", headers=agent, json=proposal(version, "diagnosis-2")).status_code == 409
    review_path = f'/api/v1/agent-operations/proposals/{row["id"]}/review'
    assert operator_request(client, "post", review_path, json={"decision": "accepted"}).status_code == 409
    operator_request(client, "delete", f"/api/v1/agent-operations/jobs/{job_id}/context")
    assert client.get(f"/api/v1/agent/jobs/{job_id}", headers=agent).status_code == 404
    assert client.get(f'/api/v1/agent/proposals/{row["id"]}', headers=agent).status_code == 404
    assert operator_request(client, "post", review_path, json={"decision": "rejected"}).status_code == 200


def test_job_change_rejects_stale_context_and_oversized_payload(api):
    client, db, job_id, agent = api
    share(client, job_id)
    job = db.get(JobReport, job_id); job.job_status = JobStatus.SUCCESS; db.commit()
    assert client.get(f"/api/v1/agent/jobs/{job_id}", headers=agent).status_code == 409
    assert client.get("/api/v1/agent/jobs", headers=agent).json()["jobs"] == []
    response = client.post(f"/api/v1/agent/jobs/{job_id}/proposals", headers=agent, content=b"x" * (6 * 1024 * 1024 + 1))
    assert response.status_code == 413


def test_proposal_rejects_secrets_and_absolute_patch_targets(api):
    client, db, job_id, agent = api
    version = share(client, job_id).json()["context_version"]
    body = proposal(version)
    path = f"/api/v1/agent/jobs/{job_id}/proposals"
    assert client.post(path, headers=agent, json={**body, "patch": "--- /etc/passwd\n+++ /etc/passwd\n"}).status_code == 422
    assert client.post(path, headers=agent, json={**body, "rationale": "api_key=unsafe"}).status_code == 422
    assert db.query(AgentTriageProposal).count() == 0


def test_reused_operator_or_ci_secret_fails_closed_for_all_protected_routes(api, monkeypatch):
    client, db, job_id, agent = api
    token = agent["Authorization"].removeprefix("Bearer ")
    monkeypatch.setenv("UI_AUTH_PASSWORD", token)
    assert client.get("/api/v1/agent/jobs", headers=agent).status_code == 503
    assert client.get("/api/v1/asoc/destinations", auth=("operator", token)).status_code == 503
    assert client.put(f"/api/v1/agent-operations/jobs/{job_id}/context", auth=("operator", token),
                      headers={"Origin": "http://testserver"}, json={"diagnostics": "hello"}).status_code == 503
    monkeypatch.setenv("UI_AUTH_PASSWORD", "different-operator-credential")
    monkeypatch.setenv("PIPE_BEARER_TOKEN", token)
    assert client.get("/api/registry", headers=agent).status_code == 503


@pytest.mark.parametrize('name', ['GITLAB', 'GITLAB_PAT'])
def test_gitlab_environment_aliases_cannot_authenticate_as_agent(api, monkeypatch, name):
    client, db, job_id, agent = api
    monkeypatch.setenv(name, agent['Authorization'].removeprefix('Bearer '))
    assert client.get('/api/v1/agent/jobs', headers=agent).status_code == 503


def test_structured_gitlab_environment_credentials_are_separated(api, monkeypatch):
    client, db, job_id, agent = api
    value = agent['Authorization'].removeprefix('Bearer ')
    monkeypatch.setenv('GITLAB_INSTANCES', json.dumps([{'host': 'gitlab.example', 'api_url': 'https://gitlab.example/api/v4', 'token': value}]))
    assert client.get('/api/v1/agent/jobs', headers=agent).status_code == 503


@pytest.mark.parametrize('kind', ['legacy', 'destination'])
def test_preexisting_database_credential_conflicts_disable_agent_access(api, kind):
    from app.integration_settings import _encrypt_secrets_with_material
    from app.models import IntegrationConfig, AsocDestination
    client, db, job_id, agent = api
    ciphertext = _encrypt_secrets_with_material({'token': agent['Authorization'].removeprefix('Bearer ')}, 'local-encryption-credential')
    if kind == 'legacy':
        db.add(IntegrationConfig(connector='asoc', enabled=True, values_json='{"url":"https://asoc.example/api"}', secrets_encrypted=ciphertext))
    else:
        db.add(AsocDestination(name='Historical fixture', provider='defectdojo', api_base_url='https://asoc.example/api',
            settings_json='{"engagement_id":"12","test_title":"scanner"}', secrets_encrypted=ciphertext, enabled=True, updated_by='operator'))
    db.commit()
    assert client.get('/api/v1/agent/jobs', headers=agent).status_code == 503


def test_new_legacy_connector_writes_reject_agent_credential_reuse(api):
    from app.integration_settings import IntegrationConfigError, save_connector
    client, db, job_id, agent = api
    with pytest.raises(IntegrationConfigError, match='differ'):
        save_connector(db, 'asoc', {'url': 'https://asoc.example/api', 'token': agent['Authorization'].removeprefix('Bearer '), 'space_id': ''}, actor='operator')
    db.rollback()


def test_disabled_unreadable_connector_does_not_block_agent_access(api):
    from app.models import IntegrationConfig
    client, db, job_id, agent = api
    db.add(IntegrationConfig(connector='asoc', enabled=False, values_json='{}', secrets_encrypted='unreadable-fixture'))
    db.commit()
    assert client.get('/api/v1/agent/jobs', headers=agent).status_code == 200
