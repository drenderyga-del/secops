from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.web import ptai as ptai_web


def _scan(
    row_id: int,
    *,
    asoc_repository_id: str | None = None,
    asoc_pipeline_id: str | None = None,
    asoc_task_id: str | None = None,
    asoc_status: str = "PENDING",
    trigger_failed: bool = False,
    ptai_stage: str | None = None,
    ptai_progress: int | None = None,
    ptai_error: str | None = None,
    error_text: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        gitlab_project_id=100 + row_id,
        gitlab_pipeline_id=200 + row_id,
        gitlab_job_id=300 + row_id,
        repository=None,
        product=None,
        asoc_scan_id=None,
        asoc_repository_id=asoc_repository_id,
        asoc_pipeline_id=asoc_pipeline_id,
        asoc_task_id=asoc_task_id,
        repo_slug=f"test/ptai-state-{row_id}",
        branch="main",
        asoc_status=asoc_status,
        trigger_failed=trigger_failed,
        job_web_url=None,
        error_text=error_text,
        last_polled_at=None,
        finished_at=None,
        last_alert_at=None,
        ptai_project_id=None,
        ptai_branch_id=None,
        ptai_scan_result_id=None,
        ptai_stage=ptai_stage,
        ptai_progress=ptai_progress,
        ptai_has_errors=False,
        ptai_error=ptai_error,
        ptai_polled_at=None,
        created_at=None,
    )


LIFECYCLE_MATRIX = [
    _scan(1),
    _scan(2, asoc_repository_id="repository-only"),
    _scan(3, asoc_pipeline_id="pipeline-only"),
    _scan(
        4,
        asoc_repository_id="repository-and-pipeline",
        asoc_pipeline_id="pipeline-with-repository",
    ),
    _scan(
        5,
        asoc_status="TRIGGER_FAILED",
        trigger_failed=True,
        error_text="ASOC did not return a scan reference",
    ),
    _scan(6, asoc_pipeline_id="running-pipeline", asoc_status="IN_PROGRESS"),
    _scan(7, asoc_pipeline_id="successful-pipeline", asoc_status="SUCCESS", ptai_stage="Done"),
    _scan(
        8,
        asoc_task_id="failed-engine-task",
        asoc_status="ERROR",
        ptai_stage="Failed",
        ptai_error="engine unavailable",
        error_text="PT AI engine failed",
    ),
]


class _FakeDatabaseService:
    def __init__(self, _db):
        pass

    def list_ptai_scans(self, **_kwargs):
        return LIFECYCLE_MATRIX, len(LIFECYCLE_MATRIX)


@pytest.fixture
def ptai_client(monkeypatch):
    monkeypatch.setattr(ptai_web, "DatabaseService", _FakeDatabaseService)
    monkeypatch.setattr(
        ptai_web,
        "_ptai_summary",
        lambda _db: {
            "total": len(LIFECYCLE_MATRIX),
            "pending": 2,
            "running": 1,
            "success": 1,
            "problems": 2,
            "last_poll": None,
        },
    )
    monkeypatch.setattr(
        ptai_web,
        "asoc_repository_url",
        lambda scan: (
            f"https://asoc.example/repositories/{scan.asoc_repository_id}"
            if scan.asoc_repository_id
            else None
        ),
    )
    monkeypatch.setattr(ptai_web, "ptai_result_url", lambda _scan: None)
    test_app = FastAPI()
    test_app.include_router(ptai_web.router)
    test_app.dependency_overrides[ptai_web.get_db] = lambda: object()
    try:
        yield TestClient(test_app)
    finally:
        test_app.dependency_overrides.clear()


def test_ptai_list_renders_reference_and_lifecycle_matrix(ptai_client):
    response = ptai_client.get("/ptai-scans/operator")

    assert response.status_code == 200
    assert response.text.count("test/ptai-state-") == len(LIFECYCLE_MATRIX)
    assert "https://asoc.example/repositories/repository-only" in response.text
    assert "No pipeline/task ID" in response.text
    assert "pipeline\u2026" in response.text
    assert "TRIGGER FAILED" in response.text
    assert "IN PROGRESS" in response.text
    assert "SUCCESS" in response.text
    assert "PT AI engine failed" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_ptai_repository_link_does_not_require_pipeline_or_task_id(ptai_client):
    response = ptai_client.get("/ptai-scans/operator")

    repository_link = 'href="https://asoc.example/repositories/repository-only"'
    assert response.status_code == 200
    assert repository_link in response.text
    assert "No pipeline/task ID" in response.text


def test_ptai_detail_renders_repository_only_reference(ptai_client, monkeypatch):
    scan = LIFECYCLE_MATRIX[1]
    monkeypatch.setattr(ptai_web, "_load_scan", lambda _db, scan_id: scan)

    response = ptai_client.get(f"/ptai-scans/{scan.id}")

    assert response.status_code == 200
    assert "test/ptai-state-2" in response.text
    assert "repository-only" in response.text
    assert "ASOC repository" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_ptai_detail_renders_representative_error_state(ptai_client, monkeypatch):
    scan = LIFECYCLE_MATRIX[-1]
    monkeypatch.setattr(ptai_web, "_load_scan", lambda _db, scan_id: scan)

    response = ptai_client.get(f"/ptai-scans/{scan.id}")

    assert response.status_code == 200
    assert "test/ptai-state-8" in response.text
    assert "PT AI engine failed" in response.text
    assert "Failed" in response.text
    assert response.headers["content-type"].startswith("text/html")
