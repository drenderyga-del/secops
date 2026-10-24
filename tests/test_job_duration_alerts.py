"""Tests for Secpipe long-running job alerting helpers."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.alerting.secpipe_job_duration import job_matches_secpipe_filter, load_default_secpipe_job_names


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
