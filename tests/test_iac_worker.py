import pytest
from pathlib import Path

from app.config_scanner.gitlab_client import GitLabAPIError
from app.iac_worker import IacWorker
from app.models.iac import IacScanRepository


def make_row(scan_status: str, has_iac: bool = True) -> IacScanRepository:
    return IacScanRepository(
        run_id=1, gitlab_project_id=1, project_path="g/p", scan_status=scan_status, has_iac=has_iac
    )


def test_run_status_counts_finder_failures_without_iac_markers():
    # Every repo failed at tree listing (has_iac stays False) — the run must
    # not report success.
    rows = [make_row("failed", has_iac=False), make_row("failed", has_iac=False)]
    assert IacWorker._run_status_for_rows(rows) == "failed"


def test_run_status_partial_when_failures_mix_with_benign_repos():
    rows = [make_row("no_iac", has_iac=False), make_row("failed", has_iac=False)]
    assert IacWorker._run_status_for_rows(rows) == "partial"
    delivered = make_row("succeeded")
    delivered.asoc_upload_status = "uploaded"
    rows = [delivered, make_row("failed")]
    assert IacWorker._run_status_for_rows(rows) == "partial"


def test_run_status_succeeded_for_clean_and_empty_runs():
    assert IacWorker._run_status_for_rows([]) == "succeeded"
    assert IacWorker._run_status_for_rows([make_row("no_iac", has_iac=False)]) == "succeeded"
    delivered = make_row("succeeded")
    delivered.asoc_upload_status = "uploaded"
    assert IacWorker._run_status_for_rows([delivered]) == "succeeded"


def test_run_status_fails_closed_when_scan_succeeds_without_delivery_confirmation():
    skipped = make_row("succeeded")
    skipped.asoc_upload_status = "skipped"
    failed = make_row("succeeded")
    failed.asoc_upload_status = "failed"

    assert IacWorker._run_status_for_rows([skipped]) == "failed"
    assert IacWorker._run_status_for_rows([failed]) == "failed"

    stale_flag = make_row("succeeded", has_iac=False)
    stale_flag.asoc_upload_status = "pending"
    assert IacWorker._run_status_for_rows([stale_flag]) == "failed"


@pytest.mark.asyncio
async def test_iac_worker_resolves_project_scope_after_group_404():
    worker = IacWorker()

    class FakeClient:
        async def list_group_projects(self, scope_path, include_subgroups=True):
            raise GitLabAPIError(
                message='GitLab API request failed: {"message":"404 Group Not Found"}',
                status_code=404,
                path=f"/groups/{scope_path}/projects",
            )

        async def get_project(self, project_path):
            assert project_path == "sandbox/appsec-templates/security_pipeline"
            return {
                "id": 101,
                "path_with_namespace": project_path,
                "web_url": f"https://gitlab.enterprise.example.com/{project_path}",
                "default_branch": "main",
            }

    projects = await worker._resolve_projects(FakeClient(), "sandbox/appsec-templates/security_pipeline", None)

    assert [project["path_with_namespace"] for project in projects] == [
        "sandbox/appsec-templates/security_pipeline"
    ]


def test_clone_and_scan_raises_clear_error_when_sarif_missing(tmp_path):
    worker = IacWorker()
    worker.work_dir = tmp_path / "work"
    worker.reports_dir = tmp_path / "reports"
    worker.clone_timeout = 1
    worker.checkov_timeout = 1
    worker.reports_dir.mkdir()

    calls = []

    def fake_run_step(label, command, **kwargs):
        calls.append(label)

    worker._run_step = fake_run_step

    with pytest.raises(RuntimeError, match="checkov did not create SARIF output"):
        worker._clone_and_scan(
            9,
            {
                "id": 101,
                "path_with_namespace": "sandbox/portal-sample-app/sample-app-ansible",
                "http_url_to_repo": "https://gitlab.enterprise.example.com/sandbox/portal-sample-app/sample-app-ansible.git",
            },
            "token",
            "main",
        )

    assert calls == ["git clone", "checkov"]


def test_clone_and_scan_copies_checkov_results_sarif_from_output_directory(tmp_path):
    worker = IacWorker()
    worker.work_dir = tmp_path / "work"
    worker.reports_dir = tmp_path / "reports"
    worker.clone_timeout = 1
    worker.checkov_timeout = 1
    worker.reports_dir.mkdir()

    def fake_run_step(label, command, **kwargs):
        if label == "checkov":
            sarif = Path(command[-1]) / "results_sarif.sarif"
            sarif.parent.mkdir(parents=True, exist_ok=True)
            sarif.write_text('{"version":"2.1.0","runs":[]}', encoding="utf-8")

    worker._run_step = fake_run_step

    sarif_path = worker._clone_and_scan(
        10,
        {
            "id": 101,
            "path_with_namespace": "sandbox/portal-sample-app/sample-app-ansible",
            "http_url_to_repo": "https://gitlab.enterprise.example.com/sandbox/portal-sample-app/sample-app-ansible.git",
        },
        "token",
        "main",
    )

    assert sarif_path == str(worker.reports_dir / "run-10-sandbox__portal-sample-app__sample-app-ansible.sarif")
    assert (worker.reports_dir / "run-10-sandbox__portal-sample-app__sample-app-ansible.sarif").read_text(
        encoding="utf-8"
    ) == '{"version":"2.1.0","runs":[]}'


def test_iac_run_status_failed_when_candidates_all_failed():
    class Row:
        has_iac = True
        scan_status = "failed"
        asoc_upload_status = "skipped"
        findings_count = 0
        critical_count = 0
        high_count = 0
        medium_count = 0
        low_count = 0
        info_count = 0

    status = IacWorker._run_status_for_rows([Row()])

    assert status == "failed"
