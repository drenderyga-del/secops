"""ASOC upload of a scan row: shared by the worker and the web re-upload
action. The function mutates the row's asoc_* fields; the caller commits."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.iac_asoc import AsocClient, AsocUploadResult
from app.models.iac import IacScanRepository

ROOT = Path(__file__).resolve().parents[1]


def make_row(**overrides) -> IacScanRepository:
    values = dict(
        run_id=1,
        gitlab_project_id=101,
        project_path="sandbox/portal/app",
        project_url="https://gitlab.enterprise.example.com/sandbox/portal/app",
        default_branch="main",
        sarif_path="/reports/run-1-app.sarif",
        error_text=None,
    )
    values.update(overrides)
    return IacScanRepository(**values)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HEXWAY_TOKEN", "test-token")
    monkeypatch.setenv("HEXWAY_URL", "https://asoc.example/api")
    return AsocClient()


def test_upload_repo_row_skips_when_client_disabled(monkeypatch):
    monkeypatch.setenv("HEXWAY_TOKEN", "")
    row = make_row()
    result = AsocClient().upload_repo_row(row, "https://gitlab.enterprise.example.com")
    assert result.status == "skipped"
    assert row.asoc_upload_status == "skipped"
    assert "ASOC is not connected in Integrations" in row.error_text
    assert row.asoc_repository_slug == "enterprise/sandbox/portal/app"


def test_upload_repo_row_creates_repository_without_asoc_product_when_no_card(client, monkeypatch):
    monkeypatch.setattr(client, "find_repository", lambda slug, project_url=None: None)
    seen = {}
    monkeypatch.setattr(
        client,
        "create_repository",
        lambda **kwargs: seen.update(kwargs) or {"id": "created", "slug": kwargs["slug"]},
    )
    monkeypatch.setattr(client, "upload_sarif", lambda **kwargs: AsocUploadResult(status="uploaded"))
    row = make_row()
    result = client.upload_repo_row(row, "https://gitlab.enterprise.example.com")
    assert result.status == "uploaded"
    assert row.asoc_upload_status == "uploaded"
    assert row.asoc_repository_id == "created"
    assert row.error_text is None
    assert seen["product_id"] is None


def test_upload_repo_row_uses_dedicated_iac_asoc_product_when_creating(client, monkeypatch):
    monkeypatch.setenv("IAC_HEXWAY_PRODUCT_ID", "iac-product-1")
    monkeypatch.setattr(client, "find_repository", lambda slug, project_url=None: None)
    seen = {}
    monkeypatch.setattr(
        client,
        "create_repository",
        lambda **kwargs: seen.update(kwargs) or {"id": "created", "slug": kwargs["slug"]},
    )
    monkeypatch.setattr(client, "upload_sarif", lambda **kwargs: AsocUploadResult(status="uploaded"))

    client.upload_repo_row(make_row(), "https://gitlab.enterprise.example.com")

    assert seen["product_id"] == "iac-product-1"


def test_create_repository_uses_known_asoc_product_id(client, monkeypatch):
    class Response:
        status_code = 201
        text = ""

        @staticmethod
        def json():
            return {"id": "created", "slug": "enterprise/sandbox/portal/app"}

    seen = {}
    monkeypatch.setattr("app.iac_asoc.requests.post", lambda *args, **kwargs: seen.update(kwargs) or Response())

    created = client.create_repository(
        slug="enterprise/sandbox/portal/app",
        project_path="sandbox/portal/app",
        project_url="https://gitlab.enterprise.example.com/sandbox/portal/app",
        product_id="asoc-product-1",
    )

    assert created["id"] == "created"
    assert seen["json"]["productID"] == "asoc-product-1"


def test_create_repository_omits_product_id_when_product_is_unknown(client, monkeypatch):
    class Response:
        status_code = 201
        text = ""

        @staticmethod
        def json():
            return {"id": "created", "slug": "enterprise/sandbox/portal/app"}

    seen = {}
    monkeypatch.setattr("app.iac_asoc.requests.post", lambda *args, **kwargs: seen.update(kwargs) or Response())

    client.create_repository(
        slug="enterprise/sandbox/portal/app",
        project_path="sandbox/portal/app",
        project_url="https://gitlab.enterprise.example.com/sandbox/portal/app",
        product_id=None,
    )

    assert "productID" not in seen["json"]


def test_upload_repo_row_uploads_into_actual_slug_of_url_matched_card(client, monkeypatch):
    monkeypatch.setattr(
        client,
        "find_repository",
        lambda slug, project_url=None: {"id": "abc", "slug": "ci/app", "_matched_by": "url"},
    )
    seen = {}

    def fake_upload_sarif(**kwargs):
        seen.update(kwargs)
        return AsocUploadResult(status="uploaded")

    monkeypatch.setattr(client, "upload_sarif", fake_upload_sarif)
    row = make_row()
    result = client.upload_repo_row(row, "https://gitlab.enterprise.example.com")
    assert result.status == "uploaded"
    assert result.matched_by == "url"
    assert seen["repository_slug"] == "ci/app"
    assert row.asoc_repository_slug == "ci/app"
    assert row.asoc_repository_id == "abc"
    assert row.asoc_upload_status == "uploaded"
    assert row.error_text is None


def test_upload_repo_row_records_failure_message(client, monkeypatch):
    monkeypatch.setattr(
        client,
        "find_repository",
        lambda slug, project_url=None: {"id": "abc", "slug": "enterprise/sandbox/portal/app", "_matched_by": "slug"},
    )
    monkeypatch.setattr(
        client,
        "upload_sarif",
        lambda **kwargs: AsocUploadResult(status="failed", message="HTTP 500: boom"),
    )
    row = make_row()
    result = client.upload_repo_row(row, "https://gitlab.enterprise.example.com")
    assert result.status == "failed"
    assert row.asoc_upload_status == "failed"
    assert "HTTP 500: boom" in row.error_text


def test_repeated_reupload_does_not_stack_identical_notes(client, monkeypatch):
    monkeypatch.setattr(
        client,
        "find_repository",
        lambda slug, project_url=None: {"id": "abc", "slug": "ci/app", "_matched_by": "url"},
    )
    monkeypatch.setattr(client, "upload_sarif", lambda **kwargs: AsocUploadResult(status="uploaded"))
    row = make_row()
    client.upload_repo_row(row, "https://gitlab.enterprise.example.com")
    client.upload_repo_row(row, "https://gitlab.enterprise.example.com")
    assert row.error_text is None


def test_reupload_route_is_registered():
    from app.routers.web.iac import router

    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/iac/repositories/{repo_row_id}/reupload-asoc" in paths


def test_repo_detail_page_has_reupload_button():
    source = (ROOT / "app/templates/iac_repository_detail.html").read_text()
    assert "/reupload-asoc" in source
    # Only offered when a SARIF exists on disk.
    assert source.index("{% if repo.sarif_path %}") < source.index("/reupload-asoc")
    assert 'name="request_key"' in source


def test_bulk_reupload_route_and_repository_selection_are_present():
    from app.routers.web.iac import router

    paths = {getattr(route, "path", None) for route in router.routes}
    template = (ROOT / "app/templates/iac_repositories.html").read_text()
    assert "/iac/repositories/reupload-asoc" in paths
    assert 'name="repo_row_ids"' in template
    assert "Re-upload selected" in template


def test_bulk_reupload_all_skipped_route_and_control_are_present():
    from app.routers.web.iac import router

    paths = {getattr(route, "path", None) for route in router.routes}
    template = (ROOT / "app/templates/iac_repositories.html").read_text()
    assert "/iac/repositories/reupload-skipped-asoc" in paths
    assert "Re-upload all skipped with SARIF" in template


def test_bulk_reupload_continues_after_one_upload_raises(monkeypatch):
    import app.routers.web.iac as iac_routes

    first = make_row()
    first.id = 1
    second = make_row(project_path="sandbox/portal/worker", project_url="https://gitlab.enterprise.example.com/sandbox/portal/worker")
    second.id = 2
    calls = []
    lifecycle_attempts = []
    events = []

    class FakeDb:
        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

    class FakeService:
        db = FakeDb()

        def get_repo_row(self, row_id):
            return {1: first, 2: second}.get(row_id)

        def get_run(self, run_id):
            return SimpleNamespace(gitlab_base_url="https://gitlab.enterprise.example.com")

        def refresh_run_counters(self, run_id):
            pass

        def sync_repository_execution(self, row, *, delivery_attempt_key):
            lifecycle_attempts.append((row.id, delivery_attempt_key, row.asoc_upload_status))
            events.append(f"sync:{row.id}")

        def prepare_delivery_attempt(self, row, *, delivery_attempt_key):
            events.append(f"prepare:{row.id}:{delivery_attempt_key}")
            row.asoc_upload_status = "pending"
            return True

    class FakeClient:
        def upload_repo_row(self, row, gitlab_base_url):
            calls.append(row.id)
            events.append(f"upload:{row.id}")
            if row.id == 1:
                raise RuntimeError("ASOC unavailable")
            return AsocUploadResult(status="uploaded")

    monkeypatch.setattr(iac_routes, "AsocClient", lambda: FakeClient())

    outcome = iac_routes._bulk_reupload_asoc_rows(FakeService(), [1, 2], request_key="request-1")

    assert calls == [1, 2]
    assert outcome == {"uploaded": 1, "skipped": 0, "failed": 1}
    assert first.asoc_upload_status == "failed"
    assert [item[0] for item in lifecycle_attempts] == [1, 2]
    assert [item[1] for item in lifecycle_attempts] == ["manual:request-1:1", "manual:request-1:2"]
    assert events.index("prepare:1:manual:request-1:1") < events.index("upload:1")
    assert events.index("prepare:2:manual:request-1:2") < events.index("upload:2")
    assert "commit" in events[: events.index("upload:1")]
