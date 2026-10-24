from __future__ import annotations

import asyncio
from collections.abc import Iterator
from urllib.parse import urlparse

import httpx
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import DatabaseService
from app.models import (
    ActivePipeline,
    Base,
    Execution,
    IntegrationConfig,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
    PipelineJob,
    PipelineStatus,
)
from app.pipeline_tracker import PipelinePoller
from app.ptai_client import PtaiClient
from app.routers.web import integrations as integrations_web
from app.schemas import PipelineRegisterRequest
from qa.mock_server import DUMMY_CREDENTIALS, LocalMockServer, SCENARIOS


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def local_only_network(monkeypatch) -> set[str]:
    """Fail immediately if production code attempts a non-loopback request."""
    observed_hosts: set[str] = set()
    original_requests = requests.sessions.Session.request
    original_httpx = httpx.AsyncClient.request

    def guarded_requests(session, method, url, *args, **kwargs):
        host = (urlparse(str(url)).hostname or "").lower()
        assert host in {"127.0.0.1", "localhost"}, f"external request blocked: {url}"
        observed_hosts.add(host)
        return original_requests(session, method, url, *args, **kwargs)

    async def guarded_httpx(client, method, url, *args, **kwargs):
        resolved = client.base_url.join(str(url))
        host = (resolved.host or "").lower()
        assert host in {"127.0.0.1", "localhost"}, (
            f"external request blocked: {resolved}"
        )
        observed_hosts.add(host)
        return await original_httpx(client, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", guarded_requests)
    monkeypatch.setattr(httpx.AsyncClient, "request", guarded_httpx)
    return observed_hosts


@pytest.fixture
def integration_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    IntegrationConfig.__table__.create(engine)
    IntegrationConfigEvent.__table__.create(engine)
    IntegrationInstanceTestState.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def pipeline_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _connector_form(key: str, base_url: str) -> dict[str, str]:
    if key == "gitlab":
        return {
            "api_url": f"{base_url}/gitlab/api/v4",
            "token": DUMMY_CREDENTIALS["gitlab"],
            "instances_json": "",
        }
    if key == "asoc":
        return {
            "url": f"{base_url}/asoc/api",
            "token": DUMMY_CREDENTIALS["asoc"],
            "space_id": "local-space",
        }
    if key == "ptai":
        return {
            "url": f"{base_url}/ptai",
            "token": DUMMY_CREDENTIALS["ptai"],
            "verify_ssl": "false",
        }
    return {
        "webhook_url": (
            f"{base_url}/mattermost/hooks/{DUMMY_CREDENTIALS['mattermost']}"
        ),
        "icon_url": "",
    }


def _assert_no_credential_leak(db, state: dict, extra_text: str = "") -> None:
    evidence = [
        extra_text,
        repr(state),
        repr(
            [
                (
                    row.connector,
                    row.changed_fields_json,
                    row.message,
                )
                for row in db.query(IntegrationConfigEvent).all()
            ]
        ),
    ]
    rows = db.query(IntegrationConfig).all()
    evidence.extend(row.secrets_encrypted or "" for row in rows)
    text = "\n".join(evidence)
    for secret in DUMMY_CREDENTIALS.values():
        assert secret not in text
    assert all(
        set(request) == {
            "method",
            "path",
            "service",
            "status",
            "request_body_bytes",
        }
        for request in state["requests"]
    )


def test_mock_control_contract_is_local_and_sanitized(local_only_network):
    with LocalMockServer() as mock:
        health = requests.get(f"{mock.base_url}/healthz", timeout=1).json()
        assert health == {"status": "ok", "outbound_requests": 0}

        configured = requests.post(
            f"{mock.base_url}/__control__/scenario",
            json={"service": "gitlab", "scenario": "rate_limited"},
            timeout=1,
        ).json()
        assert configured["scenarios"]["gitlab"] == "rate_limited"

        reset = requests.post(
            f"{mock.base_url}/__control__/reset",
            timeout=1,
        ).json()
        assert set(reset["scenarios"].values()) == {"success"}
        assert reset["outbound_requests"] == 0

    assert local_only_network == {"127.0.0.1"}


def test_pipeline_worker_reaches_terminal_state_against_real_mock(
    monkeypatch,
    pipeline_factory,
    local_only_network,
):
    with LocalMockServer() as mock:
        settings = {
            "GITLAB_API_URL": f"{mock.base_url}/gitlab/api/v4",
            "GITLAB_PAT": DUMMY_CREDENTIALS["gitlab"],
            "GITLAB_INSTANCES": "",
        }
        monkeypatch.setattr(
            "app.pipeline_tracker.runtime_setting",
            lambda name, default="": settings.get(name, default),
        )
        monkeypatch.setattr("app.pipeline_tracker.SessionLocal", pipeline_factory)

        with pipeline_factory() as db:
            pipeline, created = DatabaseService(db).register_pipeline(
                PipelineRegisterRequest(
                    project_id=101,
                    pipeline_id=7001,
                    project_path="qa/security/repo",
                    project_url=f"{mock.base_url}/gitlab/qa/security/repo",
                    ref="main",
                    commit_sha="a" * 40,
                    tracked_jobs=["sast_scan"],
                )
            )
            pipeline_id = pipeline.id
            assert created is True
            assert pipeline.status == PipelineStatus.REGISTERED

        poller = PipelinePoller()
        asyncio.run(poller._poll_once())
        with pipeline_factory() as db:
            pipeline = db.get(ActivePipeline, pipeline_id)
            job = db.query(PipelineJob).one()
            assert pipeline.status == PipelineStatus.POLLING
            assert job.status == "running"

        asyncio.run(poller._poll_once())
        with pipeline_factory() as db:
            pipeline = db.get(ActivePipeline, pipeline_id)
            job = db.query(PipelineJob).one()
            execution = (
                db.query(Execution)
                .filter(
                    Execution.correlation_key
                    == "gitlab_pipeline:101:7001"
                )
                .one()
            )
            assert pipeline.status == PipelineStatus.COMPLETED
            assert pipeline.completed_at is not None
            assert job.status == "success"
            assert execution.status == "succeeded"

        state = mock.state.snapshot()
        assert state["pipeline_polls"] == 2
        assert state["outbound_requests"] == 0
        assert local_only_network == {"127.0.0.1"}


def test_ptai_mock_matches_real_progress_client_contract(
    monkeypatch,
    local_only_network,
):
    with LocalMockServer() as mock:
        settings = {
            "PTAI_URL": f"{mock.base_url}/ptai",
            "PTAI_TOKEN": DUMMY_CREDENTIALS["ptai"],
            "PTAI_VERIFY_SSL": "false",
        }
        monkeypatch.setattr(
            "app.ptai_client.runtime_setting",
            lambda name, default="": settings.get(name, default),
        )

        status = PtaiClient().resolve_status(
            repo_slug="qa/security/repo",
            branch="main",
        )

        assert status is not None
        assert status.project_id == "pt-project-local-1"
        assert status.branch_id == "pt-branch-local-1"
        assert status.scan_result_id == "pt-result-local-1"
        assert status.stage == "Scan"
        assert status.progress == 73
        assert status.has_errors is False
        assert local_only_network == {"127.0.0.1"}


@pytest.mark.parametrize("connector", ["gitlab", "asoc", "ptai", "mattermost"])
def test_saved_connector_failure_matrix_and_recovery(
    connector,
    monkeypatch,
    integration_db,
    local_only_network,
    caplog,
):
    monkeypatch.setenv("UI_AUTH_USERNAME", "local-operator")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "local-password")
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "local-test-key")
    # Keep a clear timing gap between a healthy local round trip and the
    # injected timeout. GitLab performs three sequential read-only probes, so
    # the production minimum of 50 ms is too narrow on Docker Desktop even
    # when every mock response is immediate.
    monkeypatch.setenv("INTEGRATION_TEST_TIMEOUT_SECONDS", "0.25")
    test_app = FastAPI()
    test_app.include_router(integrations_web.router)
    test_app.dependency_overrides[integrations_web.get_db] = lambda: integration_db

    with LocalMockServer(delay_seconds=0.75) as mock, TestClient(test_app) as client:
        saved = client.post(
            f"/integrations/{connector}/save",
            data=_connector_form(connector, mock.base_url),
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert "tone=success" in saved.headers["location"]

        for scenario in sorted(SCENARIOS - {"success"}):
            mock.state.set_scenario(scenario, connector)
            failed = client.post(
                f"/integrations/{connector}/test",
                follow_redirects=False,
            )
            assert failed.status_code == 303
            assert "tone=error" in failed.headers["location"]
            row = (
                integration_db.query(IntegrationConfig)
                .filter(IntegrationConfig.connector == connector)
                .one()
            )
            assert row.test_status == "failed"

            mock.state.set_scenario("success", connector)
            recovered = client.post(
                f"/integrations/{connector}/test",
                follow_redirects=False,
            )
            assert recovered.status_code == 303
            assert "tone=success" in recovered.headers["location"]
            integration_db.refresh(row)
            assert row.test_status == "healthy"

        state = mock.state.snapshot()
        assert state["outbound_requests"] == 0
        assert all(item["service"] == connector for item in state["requests"])
        assert local_only_network == {"127.0.0.1"}
        _assert_no_credential_leak(
            integration_db,
            state,
            extra_text=caplog.text,
        )

    events = (
        integration_db.query(IntegrationConfigEvent)
        .filter(IntegrationConfigEvent.connector == connector)
        .order_by(IntegrationConfigEvent.id)
        .all()
    )
    assert [event.action for event in events] == (
        ["save"]
        + [
            action
            for _scenario in sorted(SCENARIOS - {"success"})
            for action in ("test", "test")
        ]
    )
    assert [event.success for event in events[1:]] == [
        success
        for _scenario in sorted(SCENARIOS - {"success"})
        for success in (False, True)
    ]
