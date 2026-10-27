"""Tests for Secpipe long-running job alerting helpers."""
import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import AsyncMock, MagicMock, patch

from app.alerting.secpipe_job_duration import (
    find_long_running_pipeline_jobs,
    job_matches_secpipe_filter,
    load_default_secpipe_job_names,
)
from app.models import ActivePipeline, PipelineJob, Product, Repository
from app.models.pipeline import PipelineStatus


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (Product.__table__, Repository.__table__, ActivePipeline.__table__, PipelineJob.__table__):
        table.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def test_job_matches_tracked_jobs_or_defaults():
    names = {"trivy_scan"}
    assert job_matches_secpipe_filter("semgrep_scan", ["semgrep_scan"], None, names) is True
    assert job_matches_secpipe_filter("trivy_scan", ["semgrep_scan"], None, names) is True
    assert job_matches_secpipe_filter("build", ["semgrep_scan"], None, names) is False


def test_ptai_registration_job_is_excluded_from_long_running_alerts(monkeypatch):
    monkeypatch.delenv("SECPIPE_JOB_LONG_ALERT_EXCLUDE_NAMES", raising=False)
    assert job_matches_secpipe_filter("ptai_scan", ["ptai_scan"], None, {"ptai_scan"}) is False


def test_job_matches_default_names():
    names = {"trivy_scan", "semgrep_scan"}
    assert job_matches_secpipe_filter("Trivy_Scan", None, None, names) is True
    assert job_matches_secpipe_filter("unknown_job", None, None, names) is False


def test_job_matches_regex():
    names = set()
    assert job_matches_secpipe_filter("my_secpipe_gate", None, r"secpipe", names) is True
    assert job_matches_secpipe_filter("other", None, r"secpipe", names) is False


def test_long_running_alerts_ignore_stale_pipeline_or_job_observations(db):
    product = Product(
        name="Delivery",
        path="delivery",
        gitlab_base_url="https://gitlab.example.com",
    )
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="fintech/delivery/backend/gateway/gateway-client360")
    db.add(repository)
    db.flush()

    started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    active_pipeline = ActivePipeline(
        project_id=208735,
        pipeline_id=19887039,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.POLLING,
    )
    stale_pipeline = ActivePipeline(
        project_id=208735,
        pipeline_id=19887038,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.STALE,
    )
    still_polling_pipeline = ActivePipeline(
        project_id=208735,
        pipeline_id=19887037,
        repository_id=repository.id,
        product_id=product.id,
        status=PipelineStatus.POLLING,
    )
    db.add_all([active_pipeline, stale_pipeline, still_polling_pipeline])
    db.flush()
    observed_recently = datetime.now(timezone.utc)
    observed_stale = observed_recently - timedelta(hours=2)
    db.add_all([
        PipelineJob(
            active_pipeline_id=active_pipeline.id,
            gitlab_job_id=123383162,
            name="semgrep_scan",
            status="running",
            started_at=started_at,
            last_seen_at=observed_recently,
        ),
        PipelineJob(
            active_pipeline_id=stale_pipeline.id,
            gitlab_job_id=123383161,
            name="semgrep_scan",
            status="running",
            started_at=started_at,
            last_seen_at=observed_recently,
        ),
        PipelineJob(
            active_pipeline_id=still_polling_pipeline.id,
            gitlab_job_id=123383160,
            name="semgrep_scan",
            status="running",
            started_at=started_at,
            last_seen_at=observed_stale,
        ),
    ])
    db.commit()

    candidates = find_long_running_pipeline_jobs(
        db,
        threshold_minutes=45,
        resend_minutes=0,
        regex_pattern=None,
        default_names={"semgrep_scan"},
    )

    assert [row[0].gitlab_job_id for row in candidates] == [123383162]


def test_load_default_secpipe_job_names(monkeypatch):
    monkeypatch.setenv("SECPIPE_JOB_LONG_ALERT_NAMES", "a_scan,b_scan")
    monkeypatch.delenv("AUDIT_SECURITY_JOB_NAMES", raising=False)
    assert load_default_secpipe_job_names() == {"a_scan", "b_scan"}


def test_load_default_secpipe_job_names_empty_env_uses_builtin(monkeypatch):
    monkeypatch.setenv("SECPIPE_JOB_LONG_ALERT_NAMES", "")
    monkeypatch.delenv("AUDIT_SECURITY_JOB_NAMES", raising=False)
    names = load_default_secpipe_job_names()
    assert "semgrep_scan" in names
    assert "trivy_scan" in names


def test_notify_long_running_job_mattermost_ok(monkeypatch):
    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "https://mattermost.example/hooks/abc")
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_SMTP_HOST", raising=False)

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    mock_instance = MagicMock()
    mock_instance.post = AsyncMock(return_value=mock_resp)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    async def _run():
        with patch("app.alerting.notifications.httpx.AsyncClient", return_value=mock_ctx):
            from app.alerting.notifications import notify_long_running_job

            return await notify_long_running_job(text="body", subject="subj")

    ok = asyncio.run(_run())
    assert ok is True
    mock_instance.post.assert_called_once()
