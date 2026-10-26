from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import DatabaseService
from app.main import app
from app.models import JobReport, JobStatus, Product, Repository
from app.target_navigation import (
    product_coverage_url,
    product_target_url,
    repository_target_url,
    targets_url,
)


ROOT = Path(__file__).resolve().parents[1]


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader("app/templates"))
    env.filters["localtime"] = lambda value, _fmt=None: str(value or "")
    env.filters["urlencode"] = lambda value: str(value)
    env.globals.update(
        product_coverage_url=product_coverage_url,
        product_target_url=product_target_url,
        repository_target_url=repository_target_url,
        targets_url=targets_url,
    )
    return env


def _request(path: str) -> SimpleNamespace:
    return SimpleNamespace(url=SimpleNamespace(path=path))


def _repository_row(repository_id: int = 7) -> SimpleNamespace:
    repository = SimpleNamespace(
        id=repository_id,
        path="platform/example_app/api",
        last_report_at=None,
        overall_status=SimpleNamespace(value="success"),
    )
    return SimpleNamespace(
        repository=repository,
        product=SimpleNamespace(id=3, name="ExampleApp"),
        control_coverage={"expected": 0, "configured": 0, "missing": 0},
        stats=SimpleNamespace(
            total_jobs=12,
            successful_jobs=11,
            failed_jobs=1,
            success_rate=91.7,
        ),
        pipeline_duration=SimpleNamespace(avg_label="4m", latest_label="3m"),
        job_duration=SimpleNamespace(avg_label="45s"),
        latest_jobs=[],
        config_scan=None,
    )


def test_product_target_repository_actions_name_and_open_distinct_destinations():
    html = _env().get_template("product_repositories.html").render(
        request=_request("/targets/products/3"),
        ui_auth_enabled=True,
        product=SimpleNamespace(id=3, name="ExampleApp"),
        repositories=[_repository_row()],
        pipeline_duration=SimpleNamespace(avg_label="4m", latest_label="3m", count=12),
        job_duration=SimpleNamespace(avg_label="45s"),
        config_scan_run_id=None,
    )

    assert 'href="/repos/7/jobs"' in html
    assert ">View job logs</span>" in html
    assert 'href="/targets/repositories/7"' in html
    assert ">Open Target 360</span>" in html


def test_repository_target_browser_keeps_logs_and_target_360_separate():
    html = _env().get_template("repositories.html").render(
        request=_request("/targets?level=repository"),
        ui_auth_enabled=True,
        repositories=[_repository_row()],
    )

    assert 'href="/repos/7/jobs"' in html
    assert ">View job logs</span>" in html
    assert 'href="/targets/repositories/7"' in html
    assert ">Open Target 360</span>" in html


def test_repository_job_evidence_route_is_registered_for_browser_actions():
    def collect_paths(router) -> set[str]:
        paths = set()
        for route in router.routes:
            if hasattr(route, "original_router"):
                paths.update(collect_paths(route.original_router))
            elif hasattr(route, "path"):
                paths.add(route.path)
        return paths

    assert "/repos/{repository_id}/jobs" in collect_paths(app.router)


def test_action_sources_do_not_reuse_generic_logs_label_for_target_360():
    target_templates = "\n".join(
        (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
        for name in ("product_repositories.html", "repositories.html", "repository_360.html")
    )

    assert "View job logs" in target_templates
    assert "Open Target 360" in target_templates
    assert 'href="/repos/{{ repo.id }}"' not in target_templates
    assert 'href="/repos/{{ repo_data.repository.id }}"' not in target_templates


def test_repository_job_evidence_uses_exact_repository_id_despite_path_collisions():
    engine = create_engine("sqlite://")
    for model in (Product, Repository, JobReport):
        model.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    first_product = Product(
        name="ExampleApp",
        path="example_app",
        gitlab_base_url="https://gitlab.example",
    )
    second_product = Product(
        name="Identity",
        path="identity",
        gitlab_base_url="https://gitlab.example",
    )
    db.add_all([first_product, second_product])
    db.flush()
    selected = Repository(product_id=first_product.id, path="team/api")
    same_path = Repository(product_id=second_product.id, path="team/api")
    substring = Repository(product_id=second_product.id, path="team/api-v2")
    db.add_all([selected, same_path, substring])
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            JobReport(
                repository_id=repository.id,
                job_name=f"job-{repository.id}",
                job_status=JobStatus.SUCCESS,
                finished_at=now,
            )
            for repository in (selected, same_path, substring)
        ]
    )
    failed_at = now - timedelta(days=2)
    db.add_all(
        [
            JobReport(
                repository_id=selected.id,
                job_name="resolved-job",
                ref="main",
                job_status=JobStatus.FAILED,
                finished_at=failed_at,
            ),
            JobReport(
                repository_id=selected.id,
                job_name="resolved-job",
                ref="main",
                job_status=JobStatus.SUCCESS,
                finished_at=failed_at + timedelta(hours=1),
            ),
            JobReport(
                repository_id=selected.id,
                job_name="active-job",
                ref="main",
                job_status=JobStatus.FAILED,
                finished_at=failed_at + timedelta(hours=2),
            ),
            JobReport(
                repository_id=same_path.id,
                job_name="same-path-job",
                ref="main",
                job_status=JobStatus.FAILED,
                finished_at=failed_at + timedelta(hours=3),
            ),
            JobReport(
                repository_id=substring.id,
                job_name="similar-prefix-job",
                ref="main",
                job_status=JobStatus.FAILED,
                finished_at=failed_at + timedelta(hours=4),
            ),
        ]
    )
    db.commit()

    jobs, total = DatabaseService(db).get_jobs_paginated(
        repo_filter=selected.path,
        repository_id=selected.id,
        job_filter=None,
        category_filter=None,
        status_filter=None,
        page=1,
        page_size=20,
    )

    assert total == 4
    assert {job.repository_id for job in jobs} == {selected.id}

    unresolved_jobs, unresolved_total = DatabaseService(db).get_jobs_paginated(
        repo_filter=selected.path,
        repository_id=selected.id,
        job_filter=None,
        category_filter=None,
        status_filter="failed",
        page=1,
        page_size=20,
        only_failed=True,
        timeframe="all",
    )

    assert unresolved_total == 1
    assert [(job.repository_id, job.job_name) for job in unresolved_jobs] == [
        (selected.id, "active-job")
    ]


def _equal_timestamp_active_failure_count(
    statuses: tuple[JobStatus, ...],
    *,
    ref: str | None = "main",
) -> int:
    engine = create_engine("sqlite://")
    for model in (Product, Repository, JobReport):
        model.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    product = Product(
        name="ExampleApp",
        path="example_app",
        gitlab_base_url="https://gitlab.example",
    )
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="example_app/api")
    db.add(repository)
    db.flush()
    finished_at = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    for status in statuses:
        db.add(
            JobReport(
                repository_id=repository.id,
                job_name="semgrep",
                ref=ref,
                job_status=status,
                finished_at=finished_at,
            )
        )
        db.flush()
    db.commit()

    return DatabaseService(db)._active_failed_jobs_query().count()


def test_later_ingested_equal_timestamp_success_resolves_active_failure():
    assert (
        _equal_timestamp_active_failure_count(
            (JobStatus.FAILED, JobStatus.SUCCESS)
        )
        == 0
    )


def test_later_ingested_equal_timestamp_failure_remains_active():
    assert (
        _equal_timestamp_active_failure_count(
            (JobStatus.SUCCESS, JobStatus.FAILED)
        )
        == 1
    )


def test_equal_timestamp_recovery_is_null_safe_for_missing_ref():
    assert (
        _equal_timestamp_active_failure_count(
            (JobStatus.FAILED, JobStatus.SUCCESS),
            ref=None,
        )
        == 0
    )
    assert (
        _equal_timestamp_active_failure_count(
            (JobStatus.SUCCESS, JobStatus.FAILED),
            ref=None,
        )
        == 1
    )
