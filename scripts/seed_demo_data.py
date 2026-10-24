"""Create deterministic, local-only data sets for ScanOps UI audits.

The command is deliberately fail-closed: the database name must contain
``demo`` or ``ui_refresh``.  ``--reset`` drops and recreates the schema, but
only after that check has passed.

Profiles:

* ``empty`` creates the schema without domain rows;
* ``kitchen-sink`` creates every important state and long-value edge cases;
* ``volume`` extends kitchen-sink past the UI pagination boundaries.

No seeded URL is contacted and all credential-looking values are conspicuous
demo placeholders.  To render the seeded integration credentials in the app,
run it with the ``INTEGRATION_CREDENTIALS_KEY`` recorded in the manifest.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from sqlalchemy import func, text

from app.asoc_products import enable_asoc_lifecycle_writes
from app.models import (
    ActivePipeline,
    Artifact,
    ArtifactProvenance,
    AsocProduct,
    AsocProductRepositoryMapping,
    AuditCoverageStatus,
    AuditRepository,
    AuditScanRun,
    AuditSnapshot,
    Base,
    ConfigScanFinding,
    ConfigScanRun,
    Connector,
    Delivery,
    Execution,
    ExecutionEvent,
    ExecutionStep,
    IacScanFinding,
    IacScanRepository,
    IacScanRun,
    IntegrationConfig,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
    JobReport,
    JobStatus,
    PipelineJob,
    PipelineStatus,
    Product,
    PtaiScan,
    Repository,
    RepositoryControlBinding,
    RepositoryStatus,
    ScannerInstance,
    SecurityControl,
    SessionLocal,
    WorkerHeartbeat,
    engine,
)


DEFAULT_ANCHOR = "2026-07-22T12:00:00+00:00"
DEFAULT_MANIFEST = Path("qa/artifacts/seed-manifest.json")
# Matches docker-compose.demo.yml's safe local default. It is intentionally
# public fixture material, never a production credential.
DEMO_CREDENTIAL_KEY = "scanops-demo-credentials"
LONG_ERROR = (
    "QA fixture: upstream scanner returned a deliberately long diagnostic. "
    "This text verifies wrapping, expansion, copy actions, and table overflow. "
    * 18
).strip()


@dataclass(frozen=True)
class SeedPlan:
    profile: str
    product_count: int
    repositories_per_product: int
    pipeline_count: int
    ptai_count: int
    audit_run_count: int
    config_run_count: int
    execution_count: int


PLANS = {
    "empty": SeedPlan("empty", 0, 0, 0, 0, 0, 0, 0),
    # Keeps the legacy default's 4 products / 12 repositories / 216 job rows.
    "kitchen-sink": SeedPlan("kitchen-sink", 4, 3, 28, 18, 6, 8, 18),
    # 224 repositories and 225+ records cross the current 200-row boundaries.
    "volume": SeedPlan("volume", 8, 28, 240, 230, 36, 32, 96),
}


def _database_name(database_url: str) -> str:
    return urlparse(database_url).path.rsplit("/", 1)[-1].lower()


def _assert_demo_database(database_url: str | None = None) -> str:
    url = database_url if database_url is not None else os.environ.get("DATABASE_URL", "")
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        local_host = hostname == "postgres"
        if hostname and not local_host:
            try:
                local_host = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                local_host = hostname == "localhost"
    except ValueError:
        parsed = None
        local_host = False
    database_name = parsed.path.rsplit("/", 1)[-1].lower() if parsed is not None else ""
    if (
        parsed is None
        or parsed.scheme not in {"postgresql", "postgresql+psycopg", "postgresql+psycopg2"}
        or not parsed.netloc
        or not local_host
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not database_name
        or not any(marker in database_name for marker in ("demo", "ui_refresh"))
    ):
        raise RuntimeError(
            "Refusing to seed or reset outside an explicit local demo database; "
            "use a loopback or the local Compose postgres host and a database name "
            "containing 'demo' or 'ui_refresh'"
        )
    return database_name


def _parse_anchor(value: str) -> datetime:
    normalized = value.strip()
    if normalized.lower() == "now":
        return datetime.now(timezone.utc).replace(second=0, microsecond=0)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timeline_start(
    anchor: datetime,
    *,
    slot_minutes: int,
    max_event_offset_minutes: int,
) -> datetime:
    """Place a lifecycle far enough behind its anchor to avoid future rows."""
    return anchor - timedelta(
        minutes=max_event_offset_minutes + max(slot_minutes, 0)
    )


def _config_run_counts(status: str, total_projects: int, index: int) -> tuple[int, int]:
    """Return coherent scanned/finding counts for a Config Guard fixture run."""
    if status == "queued" or total_projects <= 0:
        return 0, 0
    if status == "running":
        scanned_projects = max(1, total_projects // 2)
    elif status == "succeeded":
        scanned_projects = total_projects
    elif status == "failed":
        scanned_projects = max(1, total_projects - 2)
    elif status == "cancelled":
        scanned_projects = max(1, total_projects // 3)
    else:
        raise ValueError(f"Unsupported config scan status: {status}")
    findings_count = min(scanned_projects, 1 + index % 4)
    return scanned_projects, findings_count


def _iac_repository_state(run_status: str, repo_index: int, repository_count: int) -> dict[str, Any]:
    """Build one lifecycle-consistent IaC repository state."""
    has_iac = repo_index % 7 != 0
    processed_limit = {
        "queued": 0,
        "running": max(1, repository_count // 2),
        "succeeded": repository_count,
        "failed": repository_count,
        "cancelled": max(1, repository_count // 3),
    }.get(run_status)
    if processed_limit is None:
        raise ValueError(f"Unsupported IaC scan status: {run_status}")

    if run_status == "queued":
        finder_status = "queued"
        scan_status = "queued"
        upload_status = "pending"
    elif run_status == "running" and repo_index == processed_limit:
        finder_status = "succeeded"
        scan_status = "running" if has_iac else "no_iac"
        upload_status = "pending" if has_iac else "skipped"
    elif repo_index >= processed_limit:
        finder_status = "queued"
        scan_status = "queued"
        upload_status = "pending"
    elif not has_iac:
        finder_status = "succeeded"
        scan_status = "no_iac"
        upload_status = "skipped"
    elif run_status == "failed" and repo_index == 1:
        finder_status = "succeeded"
        scan_status = "failed"
        upload_status = "skipped"
    else:
        finder_status = "succeeded"
        scan_status = "succeeded"
        upload_status = (
            "uploaded"
            if run_status in {"succeeded", "failed", "cancelled"} or repo_index % 2 == 0
            else "uploading"
        )

    if scan_status == "succeeded":
        severity = {
            "critical_count": repo_index % 3,
            "high_count": repo_index % 4,
            "medium_count": repo_index % 5,
            "low_count": repo_index % 6,
            "info_count": repo_index % 2,
        }
    else:
        severity = {
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "low_count": 0,
            "info_count": 0,
        }
    return {
        "has_iac": has_iac,
        "finder_status": finder_status,
        "scan_status": scan_status,
        "upload_status": upload_status,
        **severity,
        "findings_count": sum(severity.values()),
    }


def _iac_run_metrics(repository_states: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate a run exactly as the production IaC service does."""
    return {
        "total_repositories": len(repository_states),
        "candidate_repositories": sum(1 for row in repository_states if row["has_iac"]),
        "scanned_repositories": sum(1 for row in repository_states if row["scan_status"] == "succeeded"),
        "uploaded_repositories": sum(1 for row in repository_states if row["upload_status"] == "uploaded"),
        "findings_count": sum(int(row["findings_count"]) for row in repository_states),
        "critical_count": sum(int(row["critical_count"]) for row in repository_states),
        "high_count": sum(int(row["high_count"]) for row in repository_states),
        "medium_count": sum(int(row["medium_count"]) for row in repository_states),
        "low_count": sum(int(row["low_count"]) for row in repository_states),
        "info_count": sum(int(row["info_count"]) for row in repository_states),
    }


def _normalize_mock_base_url(value: str | None) -> str | None:
    """Accept only explicitly local mock endpoints for interaction fixtures."""
    if value is None or not value.strip():
        return None
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    allowed_hosts = {"qa_mock", "localhost", "127.0.0.1", "host.docker.internal"}
    if (
        parsed.scheme != "http"
        or parsed.hostname not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "--mock-base-url must be an http:// URL whose host is qa_mock, "
            "localhost, 127.0.0.1, or host.docker.internal"
        )
    return normalized


def _encrypted_demo_secrets(material: str, values: dict[str, str]) -> str:
    key_material = b"scanops:integration-credentials:v1:" + material.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(key_material).digest())
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return Fernet(key).encrypt(payload).decode("ascii")


def _repo_path(product_index: int, repo_index: int) -> str:
    if product_index == 1 and repo_index == 1:
        suffix = "service-with-a-name-long-enough-to-exercise-navigation-table-and-card-overflow"
    else:
        suffix = f"service-{repo_index:03d}"
    return f"qa/product-{product_index:02d}/{suffix}"


def create_products_and_jobs(db, now: datetime, plan: SeedPlan) -> tuple[list[Product], list[Repository]]:
    products: list[Product] = []
    repositories: list[Repository] = []
    for product_index in range(1, plan.product_count + 1):
        product = Product(
            name=(
                "Identity and Access Management Platform With Deliberately Long Product Name"
                if product_index == 1
                else f"QA Product {product_index:02d}"
            ),
            path=f"qa/product-{product_index:02d}",
            gitlab_base_url="https://gitlab.demo.invalid",
            asoc_product_slug=f"qa-product-{product_index:02d}",
            asoc_product_id=f"demo-asoc-{product_index}",
            asoc_sync_status="mapped",
            created_at=now - timedelta(days=30 + product_index),
        )
        db.add(product)
        db.flush()
        products.append(product)

        for repo_index in range(1, plan.repositories_per_product + 1):
            repository = Repository(
                product_id=product.id,
                path=_repo_path(product_index, repo_index),
                last_report_at=(None if repo_index % 11 == 0 else now - timedelta(minutes=repo_index * 7)),
                overall_status=(
                    RepositoryStatus.UNKNOWN
                    if repo_index % 11 == 0
                    else RepositoryStatus.FAILED
                    if repo_index % 5 == 0
                    else RepositoryStatus.SUCCESS
                ),
                created_at=now - timedelta(days=29, minutes=product_index * 100 + repo_index),
            )
            db.add(repository)
            db.flush()
            repositories.append(repository)

    job_names = ["sast_scan", "sca_scan", "gitleaks_scan", "container_scan", "ptai_scan"]
    reports_per_repo = 18 if plan.profile == "kitchen-sink" else 5
    report_statuses = [JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.SKIPPED]
    for repo_index, repository in enumerate(repositories):
        for offset in range(reports_per_repo):
            status = report_statuses[(repo_index + offset) % len(report_statuses)]
            finished = now - timedelta(hours=(offset * 7 + repo_index) % (24 * 370))
            db.add(
                JobReport(
                    repository_id=repository.id,
                    job_name=job_names[(repo_index + offset) % len(job_names)],
                    job_status=status,
                    pipeline_url=(
                        f"https://gitlab.demo.invalid/{repository.path}/-/pipelines/"
                        f"{830000 + repo_index * 20 + offset}"
                    ),
                    ref=(
                        "feature/SECOPS-1234-a-branch-name-that-is-intentionally-long-for-layout-testing"
                        if offset == 0 and repo_index == 0
                        else "main" if offset % 3 else "release/2026.07"
                    ),
                    duration_seconds=None if status == JobStatus.RUNNING else 30 + ((repo_index * 91 + offset * 47) % 4800),
                    finished_at=finished,
                    created_at=finished,
                )
            )
    return products, repositories


def create_pipelines(db, now: datetime, plan: SeedPlan, repositories: list[Repository]) -> list[ActivePipeline]:
    rows: list[ActivePipeline] = []
    statuses = list(PipelineStatus)
    for index in range(plan.pipeline_count):
        repository = repositories[index % len(repositories)]
        status = statuses[index % len(statuses)]
        # Equal timestamps exercise deterministic secondary ordering. Some active
        # records are older than 24h to expose stale/reconcile behaviour.
        created = _timeline_start(
            now,
            slot_minutes=(index // 3) * 9,
            max_event_offset_minutes=20,
        )
        if status in {PipelineStatus.REGISTERED, PipelineStatus.POLLING} and index % 4 == 0:
            created = now - timedelta(days=3, minutes=index)
        elif status == PipelineStatus.STALE:
            created = now - timedelta(days=2, minutes=index)
        terminal = status in {PipelineStatus.COMPLETED, PipelineStatus.FAILED}
        pipeline = ActivePipeline(
            project_id=410000 + (index % len(repositories)),
            pipeline_id=930000 + index,
            repository_id=repository.id,
            product_id=repository.product_id,
            status=status,
            commit_sha=hashlib.sha256(f"demo-commit-{index}".encode()).hexdigest(),
            git_ref="main" if index % 4 else "release/a-very-long-release-candidate-branch-name-for-ui-audit",
            tracked_jobs=["sast_scan", "sca_scan", "gitleaks_scan"],
            web_url=f"https://gitlab.demo.invalid/{repository.path}/-/pipelines/{930000 + index}",
            started_at=created + timedelta(minutes=1),
            finished_at=created + timedelta(minutes=19) if terminal else None,
            duration_seconds=1080.0 if terminal else None,
            last_polled_at=None if status == PipelineStatus.REGISTERED else now - timedelta(minutes=index % 30),
            created_at=created,
            updated_at=created + timedelta(minutes=20),
            completed_at=created + timedelta(minutes=19) if terminal else None,
            stale_at=created + timedelta(days=1) if status == PipelineStatus.STALE else None,
            error_message=(LONG_ERROR if status == PipelineStatus.FAILED and index % 2 == 0 else None),
        )
        db.add(pipeline)
        db.flush()
        rows.append(pipeline)
        for job_index, job_name in enumerate(("sast_scan", "sca_scan", "gitleaks_scan"), start=1):
            job_status = (
                "failed" if status == PipelineStatus.FAILED and job_index == 3
                else "success" if terminal
                else "running" if status == PipelineStatus.POLLING
                else "created"
            )
            db.add(
                PipelineJob(
                    active_pipeline_id=pipeline.id,
                    gitlab_job_id=93000000 + index * 10 + job_index,
                    name=job_name,
                    stage="security",
                    status=job_status,
                    ref=pipeline.git_ref,
                    web_url=f"{pipeline.web_url}/jobs/{job_index}",
                    started_at=pipeline.started_at,
                    finished_at=pipeline.finished_at,
                    duration_seconds=pipeline.duration_seconds,
                    last_seen_at=pipeline.last_polled_at or created,
                )
            )
    return rows


def create_controls(db, now: datetime, repositories: list[Repository]) -> list[SecurityControl]:
    specs = [
        ("sast", "Static application security testing", "code", True),
        ("sca", "Software composition analysis", "dependencies", True),
        ("secrets", "Secret detection", "code", True),
        ("iac", "Infrastructure as Code", "infrastructure", True),
        ("legacy-dast", "Legacy dynamic testing", "runtime", False),
    ]
    controls: list[SecurityControl] = []
    for key, name, category, active in specs:
        control = SecurityControl(
            control_key=key,
            name=name,
            category=category,
            description=f"Deterministic QA control for {name}.",
            is_active=active,
            created_at=now - timedelta(days=60),
        )
        db.add(control)
        db.flush()
        controls.append(control)
    statuses = ["configured", "missing", "unknown", "excluded"]
    for repo_index, repository in enumerate(repositories):
        for control_index, control in enumerate(controls):
            status = statuses[(repo_index + control_index) % len(statuses)]
            db.add(
                RepositoryControlBinding(
                    repository_id=repository.id,
                    security_control_id=control.id,
                    scope="*" if control_index != 3 else "infra/**",
                    status=status,
                    source="qa-fixture",
                    evidence=json.dumps({"job": control.control_key, "observed": status == "configured"}),
                    reason=LONG_ERROR if status == "missing" and repo_index == 0 else f"QA state: {status}",
                    last_verified_at=now - timedelta(hours=repo_index % 48),
                    created_at=now - timedelta(days=20),
                )
            )
    return controls


def _execution_step_states(
    status: str,
    delivery_status: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return a coherent observed lifecycle for a seeded execution."""
    if status in {"requested", "queued"}:
        return ()
    if status in {"running", "cancel_requested"}:
        return (("trigger", "succeeded"), ("scan", "running"))
    if status == "delivering":
        return (
            ("trigger", "succeeded"),
            ("scan", "succeeded"),
            ("normalize", "succeeded"),
            ("deliver", "running"),
        )
    if status == "succeeded":
        return tuple(
            (name, "succeeded")
            for name in ("trigger", "scan", "normalize", "deliver")
        )
    if status == "partial":
        return (
            ("trigger", "succeeded"),
            ("scan", "succeeded"),
            ("normalize", "succeeded"),
            ("deliver", delivery_status or "failed"),
        )
    if status == "failed":
        return (("trigger", "succeeded"), ("scan", "failed"))
    if status == "cancelled":
        return (("trigger", "succeeded"), ("scan", "cancelled"))
    raise ValueError(f"Unsupported execution status: {status}")


def _execution_produces_artifact(status: str) -> bool:
    """Only lifecycles that reach normalization can emit delivery evidence."""
    return status in {"delivering", "succeeded", "partial"}


def _execution_delivery_status(status: str, index: int) -> str | None:
    if status == "delivering":
        return "running"
    if status == "succeeded":
        return "confirmed"
    if status == "partial":
        return "failed" if (index // 9) % 2 == 0 else "skipped"
    return None


def create_executions(db, now: datetime, plan: SeedPlan, repositories: list[Repository]) -> list[Execution]:
    statuses = ["requested", "queued", "running", "delivering", "cancel_requested", "succeeded", "partial", "failed", "cancelled"]
    executions: list[Execution] = []
    for index in range(plan.execution_count):
        repository = repositories[index % len(repositories)]
        status = statuses[index % len(statuses)]
        terminal = status in {"succeeded", "partial", "failed", "cancelled"}
        delivery_status = _execution_delivery_status(status, index)
        partial_reason = (
            "upload_failed"
            if delivery_status == "failed"
            else "destination_missing"
        )
        requested_at = now - timedelta(hours=index + 2)
        execution = Execution(
            repository_id=repository.id,
            product_id=repository.product_id,
            source=["gitlab_pipeline", "iac_run", "config_scan", "ptai_engine"][index % 4],
            source_scope=repository.path,
            correlation_key=f"qa-execution-{index:04d}",
            status=status,
            failure_category="execution" if status == "failed" else "delivery" if status == "partial" else None,
            failure_reason="scanner_failed" if status == "failed" else partial_reason if status == "partial" else None,
            requested_at=requested_at,
            started_at=requested_at + timedelta(minutes=1) if status not in {"requested", "queued"} else None,
            finished_at=requested_at + timedelta(minutes=24) if terminal else None,
            context_json=json.dumps({"fixture": True, "repository": repository.path}, sort_keys=True),
            created_at=requested_at,
            updated_at=requested_at + timedelta(minutes=24),
        )
        db.add(execution)
        db.flush()
        executions.append(execution)

        steps: list[ExecutionStep] = []
        for step_index, (name, step_status) in enumerate(
            _execution_step_states(status, delivery_status),
            start=1,
        ):
            step_finished = step_status in {
                "succeeded",
                "failed",
                "cancelled",
                "skipped",
            }
            step = ExecutionStep(
                execution_id=execution.id,
                source=execution.source,
                external_id=f"qa-step-{index:04d}-{step_index}",
                name=name,
                status=step_status,
                failure_category=(
                    "delivery"
                    if name == "deliver" and step_status in {"failed", "skipped"}
                    else "execution"
                    if step_status == "failed"
                    else None
                ),
                failure_reason=(
                    "upload_failed"
                    if name == "deliver" and step_status == "failed"
                    else "destination_missing"
                    if name == "deliver" and step_status == "skipped"
                    else "scanner_failed"
                    if step_status == "failed"
                    else None
                ),
                started_at=requested_at + timedelta(minutes=step_index),
                finished_at=requested_at + timedelta(minutes=step_index * 3) if step_finished else None,
                context_json=json.dumps({"ordinal": step_index}),
                created_at=requested_at + timedelta(minutes=step_index),
            )
            db.add(step)
            db.flush()
            steps.append(step)
            observed_at = requested_at + timedelta(minutes=step_index * 3)
            payload = json.dumps({"status": step_status, "step": name, "long": LONG_ERROR if index == 0 else ""})
            db.add(
                ExecutionEvent(
                    execution_id=execution.id,
                    schema_version="1.0",
                    event_id=f"qa-event-{index:04d}-{step_index}",
                    event_type="step.updated",
                    source=execution.source,
                    source_scope=repository.path,
                    correlation_key=execution.correlation_key,
                    reason=LONG_ERROR if step_status == "failed" else None,
                    observed_at=observed_at,
                    received_at=observed_at + timedelta(seconds=2),
                    payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                    payload_json=payload,
                )
            )

        if _execution_produces_artifact(status):
            artifact = Artifact(
                execution_id=execution.id,
                source=execution.source,
                external_id=f"qa-artifact-{index:04d}",
                kind="sarif",
                status="created",
                uri=f"demo://artifacts/{index:04d}/report.sarif",
                digest_sha256=hashlib.sha256(f"artifact-{index}".encode()).hexdigest(),
                size_bytes=1024 + index,
                schema_version="2.1.0",
                created_at=requested_at + timedelta(minutes=20),
            )
            db.add(artifact)
            db.flush()
            producer_step = next(step for step in steps if step.name == "normalize")
            db.add(
                ArtifactProvenance(
                    artifact_id=artifact.id,
                    execution_id=execution.id,
                    execution_step_id=producer_step.id,
                    source_revision=hashlib.sha256(f"revision-{index}".encode()).hexdigest(),
                    source_ref="main",
                    observed_at=requested_at + timedelta(minutes=20),
                    context_json=json.dumps({"producer": "qa-fixture"}),
                    created_at=requested_at + timedelta(minutes=20),
                )
            )
            db.add(
                Delivery(
                    execution_id=execution.id,
                    artifact_id=artifact.id,
                    source=execution.source,
                    destination="asoc:demo",
                    attempt_key="initial",
                    external_id=f"qa-delivery-{index:04d}",
                    status=delivery_status,
                    failure_category="delivery" if delivery_status in {"failed", "skipped"} else None,
                    failure_reason=(
                        "upload_failed"
                        if delivery_status == "failed"
                        else "destination_missing"
                        if delivery_status == "skipped"
                        else None
                    ),
                    error_text=LONG_ERROR if delivery_status == "failed" else None,
                    attempted_at=requested_at + timedelta(minutes=21),
                    confirmed_at=requested_at + timedelta(minutes=22) if delivery_status == "confirmed" else None,
                    created_at=requested_at + timedelta(minutes=21),
                )
            )
    return executions


def create_scanner_runs(db, now: datetime, plan: SeedPlan, products: list[Product], repositories: list[Repository]) -> tuple[list[ConfigScanRun], list[IacScanRun]]:
    config_runs: list[ConfigScanRun] = []
    config_statuses = ["queued", "running", "succeeded", "failed", "cancelled"]
    for index in range(plan.config_run_count):
        product = products[index % len(products)]
        status = config_statuses[index % len(config_statuses)]
        started = now - timedelta(hours=index * 2 + 1)
        product_repositories = [repository for repository in repositories if repository.product_id == product.id]
        scanned_projects, findings_count = _config_run_counts(status, len(product_repositories), index)
        run = ConfigScanRun(
            product_id=product.id,
            status=status,
            trigger=["scheduled", "manual", "poller"][index % 3],
            started_at=None if status == "queued" else started,
            finished_at=started + timedelta(minutes=43) if status in {"succeeded", "failed", "cancelled"} else None,
            total_projects=len(product_repositories),
            scanned_projects=scanned_projects,
            findings_count=findings_count,
            scope_path=product.path,
            error_text=LONG_ERROR if status == "failed" else None,
            created_at=started - timedelta(minutes=2),
        )
        db.add(run)
        db.flush()
        config_runs.append(run)
        for finding_index in range(findings_count):
            repository = product_repositories[finding_index % scanned_projects]
            db.add(
                ConfigScanFinding(
                    run_id=run.id,
                    product_id=product.id,
                    repository_id=repository.id,
                    project_path=repository.path,
                    project_url=f"https://gitlab.demo.invalid/{repository.path}",
                    severity=["critical", "high", "medium", "low"][(index + finding_index) % 4],
                    rule_id=f"CFG-{100 + index}-{finding_index + 1}",
                    title="Protected branch policy drift with a deliberately descriptive title",
                    description=LONG_ERROR if index == 1 and finding_index == 0 else "Default branch protection differs from the baseline.",
                    recommendation="Restore required approvals and protected branch settings.",
                    created_at=started,
                )
            )

    iac_runs: list[IacScanRun] = []
    iac_statuses = ["queued", "running", "succeeded", "failed", "cancelled"]
    run_count = 3 if plan.profile == "kitchen-sink" else 8
    for run_index in range(run_count):
        status = iac_statuses[run_index % len(iac_statuses)]
        started = now - timedelta(hours=run_index * 5 + 2)
        # Run zero intentionally keeps every repository so the volume profile
        # continues to exercise the >200-row UI boundary.
        run_repositories = repositories if run_index == 0 else repositories[: min(16, len(repositories))]
        repository_states = [
            _iac_repository_state(status, repo_index, len(run_repositories))
            for repo_index in range(len(run_repositories))
        ]
        metrics = _iac_run_metrics(repository_states)
        run = IacScanRun(
            status=status,
            trigger="rescan" if run_index == 1 else "manual",
            target_url="https://gitlab.demo.invalid/qa",
            gitlab_base_url="https://gitlab.demo.invalid",
            scope_path="qa",
            scanner_name="checkov",
            total_repositories=metrics["total_repositories"],
            candidate_repositories=metrics["candidate_repositories"],
            scanned_repositories=metrics["scanned_repositories"],
            uploaded_repositories=metrics["uploaded_repositories"],
            findings_count=metrics["findings_count"],
            summary_json=json.dumps(
                {
                    "repositories_total": metrics["total_repositories"],
                    "iac_repositories": metrics["candidate_repositories"],
                    "scanned_repositories": metrics["scanned_repositories"],
                    "uploaded_repositories": metrics["uploaded_repositories"],
                    "findings_total": metrics["findings_count"],
                    "severity": {
                        "critical": metrics["critical_count"],
                        "high": metrics["high_count"],
                        "medium": metrics["medium_count"],
                        "low": metrics["low_count"],
                        "info": metrics["info_count"],
                    },
                },
                sort_keys=True,
            ),
            error_text=LONG_ERROR if status == "failed" else None,
            started_at=None if status == "queued" else started,
            finished_at=started + timedelta(minutes=52) if status in {"succeeded", "failed", "cancelled"} else None,
            cancel_requested_at=started + timedelta(minutes=10) if status == "cancelled" else None,
            cancelled_at=started + timedelta(minutes=12) if status == "cancelled" else None,
            created_at=started - timedelta(minutes=1),
        )
        db.add(run)
        db.flush()
        iac_runs.append(run)
        for repo_index, (repository, repository_state) in enumerate(zip(run_repositories, repository_states)):
            row = IacScanRepository(
                run_id=run.id,
                product_id=repository.product_id,
                repository_id=repository.id,
                gitlab_project_id=520000 + run_index * 1000 + repo_index,
                project_path=repository.path,
                project_url=f"https://gitlab.demo.invalid/{repository.path}",
                default_branch="main",
                has_iac=repository_state["has_iac"],
                iac_markers="terraform,helm" if repository_state["has_iac"] else None,
                finder_status=repository_state["finder_status"],
                scan_status=repository_state["scan_status"],
                asoc_upload_status=repository_state["upload_status"],
                critical_count=repository_state["critical_count"],
                high_count=repository_state["high_count"],
                medium_count=repository_state["medium_count"],
                low_count=repository_state["low_count"],
                info_count=repository_state["info_count"],
                findings_count=repository_state["findings_count"],
                error_text=LONG_ERROR if repository_state["scan_status"] == "failed" else None,
                created_at=started,
            )
            db.add(row)
            db.flush()
            for severity in ("critical", "high", "medium", "low", "info"):
                for finding_index in range(int(repository_state[f"{severity}_count"])):
                    db.add(
                        IacScanFinding(
                            run_id=run.id,
                            scan_repository_id=row.id,
                            product_id=repository.product_id,
                            repository_id=repository.id,
                            project_path=repository.path,
                            project_url=row.project_url,
                            severity=severity,
                            rule_id=f"CKV_QA_{run_index}_{repo_index}_{severity}_{finding_index}",
                            title="Object storage access logging is disabled",
                            description=LONG_ERROR if repo_index == 1 and finding_index == 0 else "Access logging is required.",
                            recommendation="Enable access logging for the bucket.",
                            file_path="infrastructure/terraform/environments/production/storage/main.tf",
                            line=4800 + repo_index + finding_index,
                            scanner_name="checkov",
                            created_at=started,
                        )
                    )
    return config_runs, iac_runs


def create_ptai_scans(db, now: datetime, plan: SeedPlan, repositories: list[Repository]) -> list[PtaiScan]:
    states = [
        ("PENDING", "Queued", 0),
        ("IN_PROGRESS", "Scan", 64),
        ("SUCCESS", "Done", 100),
        ("ERROR", "Failed", 81),
        ("PARTIAL_SUCCESS", "Done", 100),
        ("TRIGGER_FAILED", None, None),
    ]
    scans: list[PtaiScan] = []
    for index in range(plan.ptai_count):
        status, stage, progress = states[index % len(states)]
        repository = repositories[(index * 2) % len(repositories)]
        created = _timeline_start(
            now,
            slot_minutes=(index // 3) * 13,
            max_event_offset_minutes=40,
        )
        row = PtaiScan(
            gitlab_project_id=610000 + index,
            gitlab_pipeline_id=710000 + index,
            gitlab_job_id=810000 + index,
            repository_id=repository.id,
            product_id=repository.product_id,
            asoc_scan_id=f"demo-scan-{index}" if status != "TRIGGER_FAILED" else None,
            asoc_repository_id=f"demo-repository-{index}",
            asoc_task_id=f"demo-task-{index}" if status != "TRIGGER_FAILED" else None,
            asoc_pipeline_id=f"demo-pipeline-{index}" if status != "TRIGGER_FAILED" else None,
            repo_slug=repository.path,
            branch="feature/a-deliberately-long-branch-for-table-overflow" if index == 0 else "main",
            asoc_status=status,
            trigger_failed=status == "TRIGGER_FAILED",
            job_web_url=f"https://gitlab.demo.invalid/{repository.path}/-/jobs/{810000 + index}",
            error_text=LONG_ERROR if status in {"ERROR", "TRIGGER_FAILED"} else None,
            last_polled_at=None if status == "PENDING" else now - timedelta(minutes=index % 55),
            finished_at=created + timedelta(minutes=37) if status in {"SUCCESS", "ERROR", "PARTIAL_SUCCESS"} else None,
            last_alert_at=created + timedelta(minutes=38) if status in {"ERROR", "PARTIAL_SUCCESS"} else None,
            ptai_project_id=f"demo-project-{index}",
            ptai_branch_id=f"demo-branch-{index}",
            ptai_scan_result_id=f"demo-result-{index}",
            ptai_stage=stage,
            ptai_progress=progress,
            ptai_has_errors=status in {"ERROR", "PARTIAL_SUCCESS"},
            ptai_error=LONG_ERROR if status == "ERROR" else None,
            ptai_polled_at=None if status == "PENDING" else now - timedelta(minutes=index % 55),
            created_at=created,
            updated_at=created + timedelta(minutes=40),
        )
        db.add(row)
        db.flush()
        scans.append(row)
    return scans


def create_coverage(db, now: datetime, plan: SeedPlan, products: list[Product], repositories: list[Repository]) -> tuple[list[AuditRepository], list[AuditScanRun]]:
    statuses = list(AuditCoverageStatus)
    audit_repositories: list[AuditRepository] = []
    for index, repository in enumerate(repositories):
        status = statuses[index % len(statuses)]
        row = AuditRepository(
            instance_host="gitlab.demo.invalid",
            project_id=900000 + index,
            path_with_namespace=repository.path,
            web_url=f"https://gitlab.demo.invalid/{repository.path}",
            default_branch="main",
            archived=status == AuditCoverageStatus.EXCLUDED and index % 2 == 0,
            empty_repo=index % 17 == 0,
            excluded=status == AuditCoverageStatus.EXCLUDED,
            exclude_reason="Archived demo repository" if status == AuditCoverageStatus.EXCLUDED else None,
            last_activity_at=now - timedelta(days=index % 400),
            created_at=now - timedelta(days=400),
        )
        db.add(row)
        db.flush()
        audit_repositories.append(row)
        # Two snapshots prove that the page selects the latest observation.
        for snapshot_offset in (7, 0):
            snapshot_status = statuses[(index + (1 if snapshot_offset else 0)) % len(statuses)]
            db.add(
                AuditSnapshot(
                    audit_repository_id=row.id,
                    scanned_at=now - timedelta(days=snapshot_offset, minutes=index % 90),
                    window_days=14,
                    status=snapshot_status,
                    reason="Security jobs observed" if snapshot_status == AuditCoverageStatus.COVERED else "Operator review required",
                    pipeline_count=12 if snapshot_status != AuditCoverageStatus.NO_PIPELINES else 0,
                    security_job_count=8 if snapshot_status == AuditCoverageStatus.COVERED else 0,
                    last_pipeline_at=now - timedelta(hours=index % 96),
                    error_message=LONG_ERROR if snapshot_status == AuditCoverageStatus.INACCESSIBLE and index == 2 else None,
                )
            )

    # The GitLab audit enumerates every project in a product group, including
    # repositories that never sent a security job to ScanOps.
    audit_only = (
        ("unscanned-service", AuditCoverageStatus.PIPELINE_NO_SEC, "Pipelines exist, but no security jobs found in 30 days", 12),
        ("archive-tool", AuditCoverageStatus.NO_PIPELINES, "No pipelines in the last 30 days", 0),
    )
    offset = len(repositories)
    for product_index, product in enumerate(products):
        for suffix, status, reason, pipeline_count in audit_only:
            offset += 1
            path = f"{product.path}/{suffix}"
            row = AuditRepository(
                instance_host="gitlab.demo.invalid",
                project_id=900000 + offset,
                path_with_namespace=path,
                web_url=f"https://gitlab.demo.invalid/{path}",
                default_branch="main",
                last_activity_at=now - timedelta(days=3 if pipeline_count else 120),
                created_at=now - timedelta(days=300),
            )
            db.add(row)
            db.flush()
            audit_repositories.append(row)
            db.add(
                AuditSnapshot(
                    audit_repository_id=row.id,
                    scanned_at=now - timedelta(minutes=product_index % 90),
                    window_days=30,
                    status=status,
                    reason=reason,
                    pipeline_count=pipeline_count,
                    security_job_count=0,
                    last_pipeline_at=now - timedelta(days=2) if pipeline_count else None,
                )
            )

    audit_runs: list[AuditScanRun] = []
    run_statuses = ["queued", "running", "succeeded", "failed", "cancelled"]
    for index in range(plan.audit_run_count):
        status = run_statuses[index % len(run_statuses)]
        started = now - timedelta(hours=index * 4 + 1)
        run = AuditScanRun(
            status=status,
            requested_product_ids=json.dumps([product.id for product in products[:2]]),
            selected_products=min(2, len(products)),
            total_repositories=len(repositories),
            processed_repositories=len(repositories) if status == "succeeded" else len(repositories) // 2,
            message="Coverage snapshot completed" if status == "succeeded" else f"QA audit state: {status}",
            error_message=LONG_ERROR if status == "failed" else None,
            started_at=None if status == "queued" else started,
            finished_at=started + timedelta(minutes=24) if status in {"succeeded", "failed", "cancelled"} else None,
            created_at=started - timedelta(minutes=1),
        )
        db.add(run)
        db.flush()
        audit_runs.append(run)
    return audit_repositories, audit_runs


def create_integrations_and_scanners(
    db,
    now: datetime,
    credential_key: str,
    mock_base_url: str | None = None,
) -> None:
    if mock_base_url:
        gitlab_url = f"{mock_base_url}/gitlab/api/v4"
        asoc_url = f"{mock_base_url}/asoc/api"
        ptai_url = f"{mock_base_url}/ptai"
        # Task326's qa.mock_server.DUMMY_CREDENTIALS are public local fixture
        # values. Keeping the seed aligned makes recovery views deterministic.
        mattermost_url = f"{mock_base_url}/mattermost/hooks/local-mattermost-token"
        gitlab_token = "local-gitlab-token"
        asoc_token = "local-asoc-token"
        ptai_token = "local-ptai-token"
    else:
        gitlab_url = "https://gitlab.demo.invalid/api/v4"
        asoc_url = "https://asoc.demo.invalid/api"
        ptai_url = "https://ptai.demo.invalid/api"
        mattermost_url = "https://mattermost.demo.invalid/hooks/demo-webhook"
        gitlab_token = "demo-gitlab-token-no-network-access"
        asoc_token = "demo-asoc-token-no-network-access"
        ptai_token = "demo-ptai-token-no-network-access"

    gitlab_instances = [
        {
            "id": "gl-1111111111111111",
            "name": "Primary GitLab",
            "host": urlparse(gitlab_url).netloc.lower(),
            "api_url": gitlab_url,
            "token": gitlab_token,
            "enabled": True,
            "polling": {"enabled": True},
        },
        {
            "id": "gl-2222222222222222",
            "name": "Release GitLab credential",
            "host": urlparse(gitlab_url).netloc.lower(),
            "api_url": gitlab_url,
            "token": (
                "local-gitlab-secondary-token"
                if mock_base_url
                else "demo-gitlab-secondary-token-no-network-access"
            ),
            "enabled": True,
            "polling": {"enabled": False},
        },
    ]
    specs = [
        (
            "gitlab",
            {"api_url": gitlab_url},
            {
                "token": gitlab_token,
                "instances_json": json.dumps(
                    gitlab_instances, sort_keys=True, separators=(",", ":")
                ),
            },
            True,
            None,
        ),
        (
            "asoc",
            {"url": asoc_url, "space_id": "demo-space"},
            {"token": asoc_token},
            True,
            "failed",
        ),
        (
            "ptai",
            {"url": ptai_url, "verify_ssl": "false" if mock_base_url else "true"},
            {"token": ptai_token},
            True,
            "healthy",
        ),
        (
            "mattermost",
            {"icon_url": "" if mock_base_url else "https://assets.demo.invalid/scanops-icon.png"},
            {"webhook_url": mattermost_url},
            False,
            "healthy" if mock_base_url else None,
        ),
    ]
    for index, (connector, values, secrets, enabled, test_status) in enumerate(specs):
        row = IntegrationConfig(
            connector=connector,
            values_json=json.dumps(values, sort_keys=True),
            secrets_encrypted=_encrypted_demo_secrets(credential_key, secrets),
            enabled=enabled,
            updated_by="qa-fixture",
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(minutes=10 + index),
            tested_at=now - timedelta(minutes=9 + index) if test_status else None,
            test_status=test_status,
            test_message=("Demo connection check passed" if test_status == "healthy" else "Demo upstream rejected request" if test_status else None),
        )
        db.add(row)
        db.add(
            IntegrationConfigEvent(
                connector=connector,
                action="test" if test_status else "save",
                actor="qa-fixture",
                changed_fields_json=json.dumps(sorted([*values, *secrets])),
                success=test_status != "failed",
                message=row.test_message or "Demo integration saved",
                created_at=now - timedelta(minutes=8 + index),
            )
        )

    db.add_all(
        [
            IntegrationInstanceTestState(
                connector="gitlab",
                instance_id="gl-1111111111111111",
                tested_at=now - timedelta(minutes=9),
                test_status="healthy",
                test_message="Demo primary GitLab connection passed",
                last_success_at=now - timedelta(minutes=9),
                last_success_message="Demo primary GitLab connection passed",
            ),
            IntegrationInstanceTestState(
                connector="gitlab",
                instance_id="gl-2222222222222222",
                tested_at=now - timedelta(minutes=7),
                test_status="failed",
                test_message="Demo secondary GitLab token was rejected",
                last_success_at=now - timedelta(days=1),
                last_success_message="Demo secondary GitLab previously passed",
            ),
        ]
    )

    connector_specs = [
        (
            "gitlab",
            "GitLab",
            "source",
            gitlab_url,
            "degraded",
            "One of two GitLab connection checks failed",
        ),
        ("asoc", "ASOC / Vampy", "delivery", asoc_url, "healthy" if mock_base_url else "degraded", None if mock_base_url else "Credential expires soon"),
        ("ptai", "PT AI Engine", "scanner", ptai_url, "healthy" if mock_base_url else "unhealthy", None if mock_base_url else LONG_ERROR),
        (
            "mattermost",
            "Mattermost",
            "alert",
            mattermost_url.rsplit("/hooks/", 1)[0],
            "disabled",
            "Disabled for recovery walkthrough",
        ),
    ]
    for index, (key, name, connector_type, endpoint, health, reason) in enumerate(connector_specs):
        connector = Connector(
            key=key,
            name=name,
            connector_type=connector_type,
            endpoint=endpoint,
            credential_ref=f"database://integration-configs/{key}",
            enabled=health != "disabled",
            health_status=health,
            health_reason=reason,
            last_seen_at=now - timedelta(minutes=index * 11) if health == "healthy" else None,
            refreshed_at=now - timedelta(minutes=index),
            created_at=now - timedelta(days=5),
        )
        db.add(connector)
        db.flush()
        if key == "gitlab":
            seeded_instances = [
                (
                    "gitlab:gl-1111111111111111",
                    "Primary GitLab",
                    "healthy",
                    "Demo primary GitLab connection passed",
                    now - timedelta(minutes=9),
                ),
                (
                    "gitlab:gl-2222222222222222",
                    "Release GitLab credential",
                    "failed",
                    "Demo secondary GitLab token was rejected",
                    None,
                ),
            ]
        else:
            seeded_instances = [
                (
                    f"{key}-demo-{instance_index}",
                    f"{name} demo instance {instance_index + 1}",
                    health,
                    reason,
                    connector.last_seen_at,
                )
                for instance_index in range(2 if key == "ptai" else 1)
            ]
        for instance_key, instance_name, instance_health, instance_reason, instance_seen_at in seeded_instances:
            db.add(
                ScannerInstance(
                    connector_id=connector.id,
                    instance_key=instance_key,
                    name=instance_name,
                    instance_type="scanner" if key == "ptai" else "connector",
                    endpoint=endpoint,
                    version="2026.7-demo",
                    capabilities_json=json.dumps(["repository discovery", "job polling", "artifact delivery"]),
                    health_status=instance_health,
                    health_reason=instance_reason,
                    last_seen_at=instance_seen_at,
                    created_at=now - timedelta(days=5),
                )
            )


def create_operational_rows(db, now: datetime, products: list[Product]) -> None:
    # The last product group intentionally has no ASOC card, so the registry
    # shows a group that is monitored in ScanOps but missing from ASOC.
    carded = products[:-1] if len(products) > 2 else products
    for product in products[len(carded):]:
        # Mirror what an ASOC sync records for a group no card links to.
        product.asoc_product_id = None
        product.asoc_product_slug = None
        product.asoc_sync_status = "missing_asoc"
    for index, product in enumerate(carded, start=1):
        created = now - timedelta(days=index * 40)
        group_url = f"https://gitlab.demo.invalid/{product.path}"
        db.add(
            AsocProduct(
                asoc_id=f"demo-asoc-{index}",
                name=f"{product.name} (BI_{240 + index})",
                slug=product.asoc_product_slug,
                bi_id=str(240 + index),
                title=product.name,
                description=(
                    f"{group_url}\n"
                    f"https://product.demo.invalid/product/BI_{240 + index}\n"
                    f"ScanOps-Scope: {'internal' if index % 2 else 'external'}"
                ),
                repositories_count=1,
                matched_repositories_count=1,
                mapping_state="mapped",
                mapping_detail=(
                    "1 of 1 ASOC description links "
                    "matched by exact GitLab URL."
                ),
                monitor_product_id=product.id,
                asoc_created=created,
                first_connected_at=created + timedelta(days=1),
                synced_at=now - timedelta(minutes=12),
            )
        )
        db.flush()
        db.add(AsocProductRepositoryMapping(
            asoc_id=f"demo-asoc-{index}",
            product_id=product.id,
            description_url=group_url,
        ))
    if products:
        # Cards that still need operator attention, plus one retired card kept
        # for quarterly history.
        db.add_all([
            AsocProduct(
                asoc_id="demo-asoc-no-link",
                name="Example Gateway (BI_301)",
                bi_id="301",
                title="Example Gateway",
                description="ScanOps-Scope: internal",
                repositories_count=0,
                matched_repositories_count=0,
                mapping_state="no_repositories",
                mapping_detail="ASOC product description contains no GitLab links.",
                asoc_created=now - timedelta(days=2),
                synced_at=now - timedelta(minutes=12),
            ),
            AsocProduct(
                asoc_id="demo-asoc-waiting",
                name="Example Console (BI_302)",
                bi_id="302",
                title="Example Console",
                description="https://gitlab.demo.invalid/qa/merchant-portal\nScanOps-Scope: external",
                repositories_count=1,
                matched_repositories_count=0,
                mapping_state="missing_target",
                mapping_detail="0 of 1 ASOC description links matched an exact monitored GitLab URL.",
                asoc_created=now - timedelta(days=12),
                synced_at=now - timedelta(minutes=12),
            ),
            AsocProduct(
                asoc_id="demo-asoc-retired",
                name="Legacy Example Service (BI_303)",
                bi_id="303",
                title="Legacy Example Service",
                description="https://gitlab.demo.invalid/qa/legacy-loans\nScanOps-Scope: internal",
                repositories_count=1,
                matched_repositories_count=0,
                mapping_state="removed",
                mapping_detail="ASOC product card is no longer present in the latest complete snapshot.",
                asoc_created=now - timedelta(days=230),
                first_connected_at=now - timedelta(days=229),
                is_active=False,
                removed_at=now - timedelta(days=45),
                synced_at=now - timedelta(minutes=12),
            ),
        ])
    db.add_all(
        [
            WorkerHeartbeat(name="pipeline_worker", last_beat_at=now - timedelta(seconds=18)),
            WorkerHeartbeat(name="iac_worker", last_beat_at=now - timedelta(minutes=17)),
        ]
    )


def _status_counts(db, model, field: str = "status") -> dict[str, int]:
    column = getattr(model, field)
    result: dict[str, int] = {}
    for value, count in db.query(column, func.count()).group_by(column).all():
        key = value.value if hasattr(value, "value") else str(value)
        result[key] = int(count)
    return dict(sorted(result.items()))


def build_manifest(
    db,
    *,
    profile: str,
    anchor: datetime,
    database_name: str,
    credential_key: str,
    mock_base_url: str | None = None,
) -> dict[str, Any]:
    first_product = db.query(Product).order_by(Product.id).first()
    first_asoc_card = db.query(AsocProduct).filter(AsocProduct.is_active.is_(True)).order_by(AsocProduct.asoc_id).first()
    first_repository = db.query(Repository).order_by(Repository.id).first()
    first_pipeline = db.query(ActivePipeline).order_by(ActivePipeline.id).first()
    first_ptai = db.query(PtaiScan).order_by(PtaiScan.id).first()
    first_audit = db.query(AuditScanRun).order_by(AuditScanRun.id).first()
    first_config = db.query(ConfigScanRun).order_by(ConfigScanRun.id).first()
    first_iac = db.query(IacScanRun).order_by(IacScanRun.id).first()
    first_iac_repository = db.query(IacScanRepository).order_by(IacScanRepository.id).first()
    counts = {
        "products": db.query(Product).count(),
        "repositories": db.query(Repository).count(),
        "job_reports": db.query(JobReport).count(),
        "pipelines": db.query(ActivePipeline).count(),
        "pipeline_jobs": db.query(PipelineJob).count(),
        "ptai_scans": db.query(PtaiScan).count(),
        "audit_repositories": db.query(AuditRepository).count(),
        "audit_snapshots": db.query(AuditSnapshot).count(),
        "audit_runs": db.query(AuditScanRun).count(),
        "config_runs": db.query(ConfigScanRun).count(),
        "iac_runs": db.query(IacScanRun).count(),
        "iac_repositories": db.query(IacScanRepository).count(),
        "executions": db.query(Execution).count(),
        "execution_steps": db.query(ExecutionStep).count(),
        "execution_events": db.query(ExecutionEvent).count(),
        "artifacts": db.query(Artifact).count(),
        "artifact_provenance": db.query(ArtifactProvenance).count(),
        "deliveries": db.query(Delivery).count(),
        "controls": db.query(SecurityControl).count(),
        "control_bindings": db.query(RepositoryControlBinding).count(),
        "integration_configs": db.query(IntegrationConfig).count(),
        "integration_events": db.query(IntegrationConfigEvent).count(),
        "integration_instance_test_states": db.query(IntegrationInstanceTestState).count(),
        "connectors": db.query(Connector).count(),
        "scanner_instances": db.query(ScannerInstance).count(),
    }
    detail_routes = []
    if (
        first_product
        and first_repository
        and first_audit
        and first_config
        and first_iac
        and first_iac_repository
        and first_ptai
    ):
        detail_routes = [
            {"id": "product-detail-seeded", "title": "Product target — populated", "path": f"/targets/products/{first_product.id}", "kind": "detail", "tags": ["targets", "detail"]},
            *(
                [{"id": "product-card-seeded", "title": "ASOC product card — populated", "path": f"/targets/products/asoc/{first_asoc_card.asoc_id}", "kind": "detail", "tags": ["targets", "detail", "reporting"]}]
                if first_asoc_card
                else []
            ),
            {"id": "target-360-seeded", "title": "Repository target — populated", "path": f"/targets/repositories/{first_repository.id}", "kind": "detail", "tags": ["targets", "detail"]},
            {"id": "target-jobs-seeded", "title": "Target executions — populated", "path": f"/repos/{first_repository.id}/jobs", "kind": "detail", "tags": ["executions", "detail"]},
            {"id": "coverage-product-seeded", "title": "Coverage product — mixed states", "path": f"/coverage/products/{first_product.id}", "kind": "detail", "tags": ["coverage", "detail"]},
            {"id": "coverage-run-seeded", "title": "Coverage run — populated", "path": f"/coverage/runs/{first_audit.id}", "kind": "detail", "tags": ["coverage", "detail"]},
            {"id": "config-product-seeded", "title": "Config Guard product — populated", "path": f"/config-scan/products/{first_product.id}", "kind": "detail", "tags": ["config-guard", "detail"]},
            {"id": "config-run-seeded", "title": "Config Guard run — populated", "path": f"/config-scan/runs/{first_config.id}", "kind": "detail", "tags": ["config-guard", "detail"]},
            {"id": "config-findings-seeded", "title": "Config Guard findings — populated", "path": f"/config-scan/runs/{first_config.id}/findings", "kind": "detail", "tags": ["config-guard", "detail"]},
            {"id": "iac-run-seeded", "title": "IaC run — populated", "path": f"/iac/runs/{first_iac.id}", "kind": "detail", "tags": ["iac", "detail"]},
            {"id": "iac-repository-seeded", "title": "IaC repository — populated", "path": f"/iac/repositories/{first_iac_repository.id}", "kind": "detail", "tags": ["iac", "detail"]},
            {"id": "ptai-scan-seeded", "title": "PT AI scan — populated", "path": f"/ptai-scans/{first_ptai.id}", "kind": "detail", "tags": ["ptai", "detail"]},
        ]
    return {
        "version": 1,
        "state": profile,
        "generatedAt": anchor.isoformat(),
        "routes": detail_routes,
        "schema_version": 1,
        "profile": profile,
        "anchor": anchor.isoformat(),
        "database_name": database_name,
        "network_policy": (
            f"integration traffic is restricted to local QA mock {mock_base_url}"
            if mock_base_url
            else "fixture URLs use .invalid and must never be contacted"
        ),
        "environment": {"INTEGRATION_CREDENTIALS_KEY": credential_key if profile != "empty" else None},
        "counts": counts,
        "status_counts": {
            "pipelines": _status_counts(db, ActivePipeline),
            "ptai_scans": _status_counts(db, PtaiScan, "asoc_status"),
            "audit_coverage": _status_counts(db, AuditSnapshot),
            "audit_runs": _status_counts(db, AuditScanRun),
            "config_runs": _status_counts(db, ConfigScanRun),
            "iac_runs": _status_counts(db, IacScanRun),
            "executions": _status_counts(db, Execution),
        },
        "urls": {
            "overview": "/",
            "targets": "/targets?level=product",
            "products": "/targets?level=product",
            "product_detail": f"/targets/products/{first_product.id}" if first_product else None,
            "repository_detail": f"/targets/repositories/{first_repository.id}" if first_repository else None,
            "pipelines": "/pipelines",
            "pipeline_probe": f"/pipelines?pipeline_id={first_pipeline.pipeline_id}" if first_pipeline else None,
            "coverage": "/coverage",
            "coverage_product": f"/coverage/products/{first_product.id}" if first_product else None,
            "coverage_run": f"/coverage/runs/{first_audit.id}" if first_audit else None,
            "config_scan": "/config-scan",
            "config_run": f"/config-scan/runs/{first_config.id}" if first_config else None,
            "iac": "/iac",
            "iac_run": f"/iac/runs/{first_iac.id}" if first_iac else None,
            "ptai": "/ptai-scans",
            "ptai_detail": f"/ptai-scans/{first_ptai.id}" if first_ptai else None,
            "scanners": "/scanners",
            "integrations": "/integrations",
            "reports": "/targets?level=product",
        },
        "expectations": {
            "volume_boundaries": {
                "pipelines_over_200": counts["pipelines"] > 200,
                "ptai_scans_over_200": counts["ptai_scans"] > 200,
                "iac_repositories_over_200": counts["iac_repositories"] > 200,
                "repositories_over_100": counts["repositories"] > 100,
            },
            "long_values_present": profile != "empty",
            "equal_sort_timestamps_present": profile != "empty",
            "all_urls_are_local_paths": True,
        },
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PLANS), default="kitchen-sink")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate the schema (demo DB only)")
    parser.add_argument("--anchor", default=DEFAULT_ANCHOR, help="Deterministic ISO-8601 UTC anchor")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--credentials-key",
        default=DEMO_CREDENTIAL_KEY,
        help="Demo-only key also required by the app to decrypt fixture credentials",
    )
    parser.add_argument(
        "--mock-base-url",
        help="Optional local-only QA integration server, for example http://qa_mock:18080",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    database_name = _assert_demo_database()
    anchor = _parse_anchor(args.anchor)
    mock_base_url = _normalize_mock_base_url(args.mock_base_url)
    plan = PLANS[args.profile]

    if args.reset:
        Base.metadata.drop_all(bind=engine)
        # schema_migrations is created outside SQLAlchemy metadata. Drop it as
        # part of the demo-only reset too, or the next startup would believe
        # that lifecycle SQL migrations still exist after their triggers and
        # guard tables were dropped with the ORM schema.
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
        from app.main import _setup_database

        _setup_database()
    else:
        Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Product).count() or db.query(IntegrationConfig).count():
            print("Demo database already seeded; use --reset to replace it")
            return

        if plan.profile != "empty":
            products, repositories = create_products_and_jobs(db, anchor, plan)
            create_pipelines(db, anchor, plan, repositories)
            create_controls(db, anchor, repositories)
            create_executions(db, anchor, plan, repositories)
            create_scanner_runs(db, anchor, plan, products, repositories)
            create_ptai_scans(db, anchor, plan, repositories)
            create_coverage(db, anchor, plan, products, repositories)
            create_integrations_and_scanners(db, anchor, args.credentials_key, mock_base_url)
            enable_asoc_lifecycle_writes(db)
            create_operational_rows(db, anchor, products)
        db.commit()

        manifest = build_manifest(
            db,
            profile=plan.profile,
            anchor=anchor,
            database_name=database_name,
            credential_key=args.credentials_key,
            mock_base_url=mock_base_url,
        )
        _write_manifest(args.manifest, manifest)
        print(
            f"Demo database seeded with profile={plan.profile}: "
            f"{manifest['counts']['products']} products, "
            f"{manifest['counts']['repositories']} repositories, "
            f"{manifest['counts']['pipelines']} pipelines; manifest={args.manifest}"
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
