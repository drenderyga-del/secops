"""Tests for async PT AI scan capture and ASOC status alerting."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ptai_capture import is_ptai_job, parse_scan_ids, ptai_job_names
from app.alerting.ptai_scan_status import (
    asoc_public_url,
    asoc_repository_url,
    build_failed_scan_alert_text,
    enrich_ptai_engine,
    fetch_status,
    normalize_asoc_status,
    status_from_ptai_engine,
)
from app.database import DatabaseService
from app.alerting.ptai_queue import build_queue_alert_text, collect_ptai_queue
from app.ptai_client import PtaiClient, PtaiEngineStatus, ptai_result_url


# ── trace parsing ────────────────────────────────────────────────────


def test_parse_scan_ids_full_line():
    trace = (
        "[INFO] Trigger Vampy PT AI scan for primary/foo @ main\n"
        "[INFO] Vampy response HTTP 200\n"
        "[INFO] Vampy scan accepted: scan_id=0f82d887-2d62-446d-8fed-1cfff8cc16ce, "
        "task_id=caf191dc-61da-471e-8c29-d52d816a9e22, "
        "pipeline_id=2835e7dc-be35-4521-afd3-9697f765f45c, repository=primary/foo/bar\n"
    )
    ids = parse_scan_ids(trace)
    assert ids["scan_id"] == "0f82d887-2d62-446d-8fed-1cfff8cc16ce"
    assert ids["task_id"] == "caf191dc-61da-471e-8c29-d52d816a9e22"
    assert ids["pipeline_id"] == "2835e7dc-be35-4521-afd3-9697f765f45c"
    assert ids["repository"] == "primary/foo/bar"


def test_parse_scan_ids_keeps_asoc_repository_id():
    ids = parse_scan_ids("[INFO] Vampy scan accepted: scan_id=scan-1, repository_id=repo-1, repository=primary/foo")
    assert ids["scan_id"] == "scan-1"
    assert ids["repository_id"] == "repo-1"


def test_parse_scan_ids_partial():
    ids = parse_scan_ids("[INFO] Vampy scan accepted: task_id=abc, pipeline_id=def")
    assert ids == {"task_id": "abc", "pipeline_id": "def"}


def test_parse_scan_ids_none_when_trigger_failed():
    assert parse_scan_ids("[ERROR] Vampy scan trigger failed") == {}
    assert parse_scan_ids(None) == {}
    assert parse_scan_ids("") == {}


def test_ptai_job_names_env(monkeypatch):
    monkeypatch.delenv("PTAI_SCAN_JOB_NAMES", raising=False)
    assert "ptai_scan" in ptai_job_names()
    assert is_ptai_job("ptai_scan") is True
    assert is_ptai_job("PTAI_SCAN") is True
    assert is_ptai_job("semgrep_scan") is False

    monkeypatch.setenv("PTAI_SCAN_JOB_NAMES", "ptai_scan, ptai_async")
    assert is_ptai_job("ptai_async") is True


# ── status resolution ────────────────────────────────────────────────


def test_fetch_status_prefers_pipeline_endpoint():
    client = MagicMock()
    client.get_scan_pipeline_status.return_value = {
        "status": "SUCCESS",
        "finished": "2026-06-25T10:00:00+00:00",
        "created": "2026-06-25T09:00:00+00:00",
    }
    scan = SimpleNamespace(asoc_pipeline_id="pid", asoc_task_id="tid")
    result = fetch_status(client, scan)
    assert result["status"] == "SUCCESS"
    assert result["source"] == "asoc_pipeline"
    assert result["raw_status"] == "SUCCESS"
    client.get_scan_pipeline_status.assert_called_once_with("pid")
    client.get_background_task.assert_not_called()


def test_fetch_status_normalizes_asoc_done():
    client = MagicMock()
    client.get_scan_pipeline_status.return_value = {
        "status": "Done",
        "finished": "2026-07-08T07:03:00+00:00",
        "created": "2026-07-08T07:02:00+00:00",
    }
    scan = SimpleNamespace(asoc_pipeline_id="pipe-done", asoc_task_id=None)

    result = fetch_status(client, scan)

    assert result["raw_status"] == "Done"
    assert result["status"] == "SUCCESS"


def test_fetch_status_falls_back_to_task():
    client = MagicMock()
    client.get_scan_pipeline_status.return_value = None
    client.get_background_task.return_value = {
        "status": "FAILED",
        "updated": "2026-06-25T10:00:00+00:00",
        "errorDetails": "boom",
    }
    scan = SimpleNamespace(asoc_pipeline_id=None, asoc_task_id="tid")
    result = fetch_status(client, scan)
    assert result["status"] == "ERROR"  # FAILED -> ERROR
    assert result["errorDetails"] == "boom"


def test_fetch_status_none_when_no_ids():
    client = MagicMock()
    scan = SimpleNamespace(asoc_pipeline_id=None, asoc_task_id=None)
    assert fetch_status(client, scan) is None


def test_normalize_asoc_status_common_values():
    assert normalize_asoc_status("Done") == "SUCCESS"
    assert normalize_asoc_status("Queued") == "PENDING"
    assert normalize_asoc_status("Progress") == "IN_PROGRESS"
    assert normalize_asoc_status("Failed") == "ERROR"


def test_ptai_repository_search_does_not_reference_missing_repository_name():
    db = MagicMock()
    query = db.query.return_value
    query.outerjoin.return_value.filter.return_value.count.return_value = 0
    query.outerjoin.return_value.filter.return_value.options.return_value.order_by.return_value.limit.return_value.offset.return_value.all.return_value = []

    rows, total = DatabaseService(db).list_ptai_scans(repo="dbo_sme")

    assert rows == []
    assert total == 0


def test_asoc_public_url_is_derived_from_hexway_url(monkeypatch):
    monkeypatch.setenv("HEXWAY_URL", "https://asoc.example/api")
    assert asoc_public_url() == "https://asoc.example"

    monkeypatch.setenv("HEXWAY_URL", "https://asoc.example/custom")
    assert asoc_public_url() == "https://asoc.example/custom"


def test_asoc_repository_url_does_not_use_vampy_request_id_as_scan_id(monkeypatch):
    monkeypatch.setenv("HEXWAY_URL", "https://asoc.example/api")
    scan = SimpleNamespace(asoc_repository_id="repo-1", asoc_scan_id="scan-1")
    assert asoc_repository_url(scan) == "https://asoc.example/repositories/repo-1"


def test_ptai_result_url_targets_saved_branch_scan_result(monkeypatch):
    monkeypatch.setenv("PTAI_URL", "https://ptai.example/")
    monkeypatch.delenv("PTAI_RESULT_URL_TEMPLATE", raising=False)
    scan = SimpleNamespace(ptai_project_id="project-1", ptai_branch_id="branch/1", ptai_scan_result_id="result-1")
    assert ptai_result_url(scan) == "https://ptai.example/ui/projects/project-1/branches/branch%2F1/scans/result-1"


def test_ptai_result_url_strips_api_suffix_from_runtime_api_origin(monkeypatch):
    monkeypatch.setenv("PTAI_URL", "https://ptai.example/api/")
    monkeypatch.delenv("PTAI_RESULT_URL_TEMPLATE", raising=False)
    scan = SimpleNamespace(ptai_project_id="project-1", ptai_branch_id="branch-1", ptai_scan_result_id="result-1")
    assert ptai_result_url(scan) == "https://ptai.example/ui/projects/project-1/branches/branch-1/scans/result-1"


def test_ptai_result_url_supports_installation_specific_ui_template(monkeypatch):
    monkeypatch.setenv("PTAI_URL", "https://ptai.example")
    monkeypatch.setenv(
        "PTAI_RESULT_URL_TEMPLATE",
        "{base_url}/ui/projects/{project_id}/branches/{branch_id}/results/{scan_result_id}",
    )
    scan = SimpleNamespace(ptai_project_id="project-1", ptai_branch_id="branch-1", ptai_scan_result_id="result-1")
    assert ptai_result_url(scan) == "https://ptai.example/ui/projects/project-1/branches/branch-1/results/result-1"


def test_ptai_result_url_uses_database_managed_runtime_settings():
    def runtime_value(name, default=""):
        return {
            "PTAI_URL": "https://db-managed-ptai.example",
            "PTAI_RESULT_URL_TEMPLATE": "{base_url}/ui/{project_id}/{branch_id}/{scan_result_id}",
        }.get(name, default)

    scan = SimpleNamespace(ptai_project_id="project-1", ptai_branch_id="branch-1", ptai_scan_result_id="result-1")
    with patch("app.ptai_client.runtime_setting", side_effect=runtime_value):
        assert ptai_result_url(scan) == "https://db-managed-ptai.example/ui/project-1/branch-1/result-1"


def test_ptai_result_url_requires_engine_result_identity(monkeypatch):
    monkeypatch.setenv("PTAI_URL", "https://ptai.example")
    scan = SimpleNamespace(ptai_project_id="project-1", ptai_branch_id=None, ptai_scan_result_id=None)
    assert ptai_result_url(scan) is None


def test_failed_alert_distinguishes_asoc_failure_after_ptai_success(monkeypatch):
    monkeypatch.setenv("SECPIPE_PUBLIC_URL", "https://monitor.example")
    monkeypatch.setenv("HEXWAY_URL", "https://asoc.example/api")
    monkeypatch.setenv("PTAI_URL", "https://ptai.example")
    monkeypatch.delenv("PTAI_RESULT_URL_TEMPLATE", raising=False)
    scan = SimpleNamespace(
        id=42,
        repo_slug="ci/dbo_sme",
        repository=None,
        branch="refs/merge-requests/3294/head",
        ptai_stage="Done",
        ptai_has_errors=False,
        ptai_error=None,
        ptai_project_id="project-1",
        ptai_branch_id="branch-1",
        ptai_scan_result_id="result-1",
        job_web_url="https://gitlab.example/jobs/7",
        asoc_pipeline_id="pipe-1",
        asoc_task_id="task-1",
        asoc_repository_id="repo-1",
        asoc_scan_id="scan-1",
    )

    text = build_failed_scan_alert_text(scan, "ERROR", "ASOC delivery failed")

    assert "ASOC processing failed after PT AI completed" in text
    assert "[open job](https://gitlab.example/jobs/7)" in text
    assert "[PT AI scan #42](https://monitor.example/ptai-scans/42)" in text
    assert "[open result](https://ptai.example/ui/projects/" in text
    assert "[open repository](https://asoc.example/repositories/repo-1)" in text
    assert "`pipe-1`" in text


# ── PT AI engine enrichment ──────────────────────────────────────────


def test_ptai_project_name_for(monkeypatch):
    monkeypatch.delenv("PTAI_PROJECT_PREFIX", raising=False)
    assert PtaiClient().project_name_for("primary/foo/bar") == "vampy-asoc-primary/foo/bar"
    monkeypatch.setenv("PTAI_PROJECT_PREFIX", "x-")
    assert PtaiClient().project_name_for("enterprise/baz") == "x-enterprise/baz"


def test_enrich_ptai_engine_sets_fields():
    from datetime import datetime, timezone

    client = MagicMock()
    client.resolve_status.return_value = PtaiEngineStatus(
        project_id="p1", branch_id="b1", scan_result_id="s1",
        stage="Scan", progress=42, has_errors=False, error=None,
    )
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    scan = SimpleNamespace(
        id=1, repo_slug="primary/foo", branch="main",
        ptai_project_id=None, ptai_branch_id=None, ptai_scan_result_id=None,
        ptai_stage=None, ptai_progress=None, ptai_has_errors=None,
        ptai_error=None, ptai_polled_at=None,
    )
    enrich_ptai_engine(client, scan, now)
    assert scan.ptai_project_id == "p1"
    assert scan.ptai_stage == "Scan"
    assert scan.ptai_progress == 42
    assert scan.ptai_polled_at == now
    # cached ids are passed back on the next call to skip name/branch lookups
    client.resolve_status.assert_called_once_with(
        repo_slug="primary/foo", branch="main", project_id=None, branch_id=None,
    )


def test_enrich_ptai_engine_no_slug_is_noop():
    client = MagicMock()
    scan = SimpleNamespace(repo_slug=None)
    enrich_ptai_engine(client, scan, None)
    client.resolve_status.assert_not_called()


def test_status_from_ptai_engine_maps_terminal_stages():
    assert status_from_ptai_engine(SimpleNamespace(ptai_stage="Done", ptai_has_errors=False)) == "SUCCESS"
    assert status_from_ptai_engine(SimpleNamespace(ptai_stage="Done", ptai_has_errors=True)) == "PARTIAL_SUCCESS"
    assert status_from_ptai_engine(SimpleNamespace(ptai_stage="Failed", ptai_has_errors=False)) == "ERROR"
    assert status_from_ptai_engine(SimpleNamespace(ptai_stage="Aborted", ptai_has_errors=False)) == "ERROR"
    assert status_from_ptai_engine(SimpleNamespace(ptai_stage="Scan", ptai_has_errors=False)) == "IN_PROGRESS"
    assert status_from_ptai_engine(SimpleNamespace(ptai_stage=None, ptai_has_errors=False)) is None


def test_cycle_uses_ptai_engine_status_when_asoc_is_unavailable(monkeypatch):
    from app.alerting import ptai_scan_status as mod

    class EmptyQuery:
        def filter(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def all(self):
            return []

    class FakeDb:
        def __init__(self):
            self.committed = False

        def query(self, *args, **kwargs):
            return EmptyQuery()

        def add(self, item):
            pass

        def commit(self):
            self.committed = True

    scan = SimpleNamespace(
        id=1,
        asoc_pipeline_id="pipe-1",
        asoc_task_id="task-1",
        asoc_status="PENDING",
        error_text=None,
        last_polled_at=None,
        finished_at=None,
        last_alert_at=None,
        repo_slug="primary/foo",
        branch="main",
        created_at=None,
        ptai_project_id=None,
        ptai_branch_id=None,
        ptai_scan_result_id=None,
        ptai_stage=None,
        ptai_progress=None,
        ptai_has_errors=None,
        ptai_error=None,
        ptai_polled_at=None,
    )
    fake_service = MagicMock()
    fake_service.fetch_pending_ptai_scans.return_value = [scan]

    fake_asoc = MagicMock()
    fake_asoc.enabled.return_value = True
    fake_asoc.get_scan_pipeline_status.return_value = None
    fake_asoc.get_background_task.return_value = None

    fake_ptai = MagicMock()
    fake_ptai.enabled.return_value = True
    fake_ptai.resolve_status.return_value = PtaiEngineStatus(
        project_id="p1",
        branch_id="b1",
        scan_result_id="s1",
        stage="Done",
        progress=100,
        has_errors=False,
        error=None,
    )

    monkeypatch.setenv("PTAI_URL", "https://ptai.example")
    monkeypatch.setenv("PTAI_TOKEN", "token")
    monkeypatch.delenv("MATTERMOST_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_SMTP_HOST", raising=False)

    with patch.object(mod, "DatabaseService", return_value=fake_service), \
         patch.object(mod, "AsocClient", return_value=fake_asoc), \
         patch.object(mod, "PtaiClient", return_value=fake_ptai), \
         patch.object(mod, "_observe_shared_execution") as observe_shared:
        sent = asyncio.run(mod.run_ptai_scan_status_cycle(FakeDb()))

    assert sent == 0
    assert scan.asoc_status == "SUCCESS"
    assert scan.error_text is None
    assert scan.finished_at is not None
    observe_shared.assert_called_once()


def test_cycle_polls_status_without_alert_channel(monkeypatch):
    from app.alerting import ptai_scan_status as mod

    class EmptyQuery:
        def filter(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def all(self):
            return []

    class FakeDb:
        def __init__(self):
            self.added = []
            self.committed = False

        def query(self, *args, **kwargs):
            return EmptyQuery()

        def add(self, item):
            self.added.append(item)

        def commit(self):
            self.committed = True

    scan = SimpleNamespace(
        id=1,
        asoc_pipeline_id="pipe-1",
        asoc_task_id="task-1",
        asoc_status="PENDING",
        error_text="ASOC status unavailable for pipe-1",
        last_polled_at=None,
        finished_at=None,
        last_alert_at=None,
        repo_slug="primary/foo",
        branch="main",
        created_at=None,
    )
    fake_service = MagicMock()
    fake_service.fetch_pending_ptai_scans.return_value = [scan]
    fake_client = MagicMock()
    fake_client.enabled.return_value = True
    fake_client.get_scan_pipeline_status.return_value = {
        "status": "SUCCESS",
        "finished": "2026-06-25T10:00:00+00:00",
        "created": "2026-06-25T09:00:00+00:00",
    }

    monkeypatch.delenv("MATTERMOST_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_SMTP_HOST", raising=False)
    monkeypatch.delenv("PTAI_URL", raising=False)
    monkeypatch.delenv("PTAI_TOKEN", raising=False)
    db = FakeDb()

    with patch.object(mod, "DatabaseService", return_value=fake_service), \
         patch.object(mod, "AsocClient", return_value=fake_client), \
         patch.object(mod, "_observe_shared_execution"):
        sent = asyncio.run(mod.run_ptai_scan_status_cycle(db))

    assert sent == 0
    assert scan.asoc_status == "SUCCESS"
    assert scan.error_text is None
    assert scan.last_polled_at is not None
    assert db.committed is True


def test_collect_ptai_queue_counts_projects_once():
    client = MagicMock()
    client.list_projects.return_value = [
        {"id": "p1", "name": "one"},
        {"id": "p2", "name": "two"},
    ]
    client.list_branches.side_effect = lambda project_id: {
        "p1": [{"id": "b1", "name": "main"}, {"id": "b2", "name": "dev"}],
        "p2": [{"id": "b3", "name": "main"}],
    }[project_id]
    client.get_last_scan_result.side_effect = lambda branch_id: {
        "b1": {"progress": {"stage": "Enqueued", "value": 0}},
        "b2": {"progress": {"stage": "Enqueued", "value": 0}},
        "b3": {"progress": {"stage": "Scan", "value": 50}},
    }[branch_id]

    queued = collect_ptai_queue(
        client,
        queue_stages={"enqueued", "queued"},
        project_limit=100,
        branch_limit=10,
    )

    assert len(queued) == 1
    assert queued[0].project_name == "one"
    assert queued[0].branch_name == "main"


def test_build_queue_alert_text():
    queued = [
        SimpleNamespace(project_name="one", branch_name="main", stage="Enqueued", progress=0),
        SimpleNamespace(project_name="two", branch_name="release", stage="Queued", progress=None),
    ]

    text = build_queue_alert_text(queued, threshold=2)

    assert "2 queued project" in text
    assert "`one` / `main`" in text
    assert "`two` / `release`" in text


# ── full cycle (needs DB) ────────────────────────────────────────────


@pytest.mark.needs_db
def test_cycle_alerts_on_failed_scan(monkeypatch):
    from app.models import PtaiScan, SessionLocal
    from app.alerting import ptai_scan_status as mod

    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "https://mm.example/hooks/x")
    monkeypatch.setenv("PTAI_SCAN_STATUS_ALERT_ENABLED", "true")

    fake_client = MagicMock()
    fake_client.enabled.return_value = True
    fake_client.get_scan_pipeline_status.return_value = {"status": "ERROR", "finished": None, "created": None}
    fake_client.get_background_task.return_value = {"errorDetails": "scanner crashed"}

    notify = AsyncMock(return_value=True)

    with SessionLocal() as db:
        scan = PtaiScan(
            gitlab_project_id=1,
            gitlab_pipeline_id=2,
            gitlab_job_id=999001,
            asoc_pipeline_id="pid-err",
            asoc_task_id="tid-err",
            repo_slug="primary/foo",
            branch="main",
            asoc_status="PENDING",
        )
        db.add(scan)
        db.commit()
        scan_id = scan.id

        with patch.object(mod, "AsocClient", return_value=fake_client), \
             patch.object(mod, "notify_long_running_job", notify):
            sent = asyncio.run(mod.run_ptai_scan_status_cycle(db))

        assert sent == 1
        notify.assert_awaited()
        refreshed = db.get(PtaiScan, scan_id)
        assert refreshed.asoc_status == "ERROR"
        assert refreshed.last_alert_at is not None
        db.delete(refreshed)
        db.commit()
