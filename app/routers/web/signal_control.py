"""Stable JSON view models for the Signal Control React application.

The presentation layer must not know about ORM tables or duplicate domain
rules.  These endpoints assemble existing ScanOps read models into compact
JSON contracts while preserving links to the canonical operational routes.
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, joinedload

from ...audit_service import AuditService
from ...asoc_products import (
    AsocSyncConfirmationRequiredError,
    AsocSyncInProgressError,
    asoc_sync_metadata,
    fetch_and_sync,
    record_sync_outcome,
    sync_result_message,
)
from ...alerting.ptai_scan_status import asoc_repository_url
from ...connector_registry import refresh_registry
from ...control_coverage import coverage_by_repository
from ...database import DatabaseService
from ...integration_settings import (
    CONNECTOR_SPECS,
    connector_snapshot,
    credential_store_ready,
    runtime_setting,
)
from ...models import (
    ActivePipeline,
    Artifact,
    ConfigScanFinding,
    ConfigScanRun,
    Connector,
    Execution,
    IacScanFinding,
    IacScanRepository,
    IacScanRun,
    JobReport,
    PTAI_ENGINE_TERMINAL_STAGES,
    PTAI_TERMINAL_STATUSES,
    Product,
    PipelineJob,
    PtaiScan,
    Repository,
    ScannerInstance,
    WorkerHeartbeat,
)
from ...models.job_report import JobStatus
from ...pipeline_query_service import PipelineQueryService, normalize_pipeline_filters
from ...ptai_client import ptai_result_url
from ...ptai_outcome import resolve_ptai_outcome
from ...target_360_service import Target360Service
from ...url_safety import safe_external_http_url
from .common import get_db

router = APIRouter(tags=["signal-control"])
logger = logging.getLogger(__name__)


class AsocSyncRequest(BaseModel):
    confirm_mass_removal: bool = False
    expected_active_count: int | None = Field(default=None, ge=0)
    expected_incoming_count: int | None = Field(default=None, ge=0)
    expected_registry_version: str | None = Field(default=None, min_length=64, max_length=64)

_IAC_TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "failed", "cancelled"}
)
_IAC_EVIDENCE_STATUSES = frozenset(
    {"succeeded", "partial", "failed"}
)
_IAC_ACTIVE_STATUSES = frozenset(
    {"queued", "requested", "registered", "running", "polling", "processing", "in_progress"}
)
_PTAI_TERMINAL_ASOC_STATUSES = frozenset(
    {*(status.upper() for status in PTAI_TERMINAL_STATUSES), "FAILED"}
)
_PTAI_TERMINAL_ENGINE_STAGES = frozenset(
    stage.lower() for stage in PTAI_ENGINE_TERMINAL_STAGES
)

_PIPELINE_FAILED_STATUSES = frozenset({"failed"})
_PIPELINE_RUNNING_STATUSES = frozenset({"running"})
_PIPELINE_WAITING_STATUSES = frozenset(
    {"created", "pending", "queued", "waiting", "manual"}
)
_PIPELINE_WARNING_STATUSES = frozenset({"canceled", "cancelled"})
_PIPELINE_COMPLETED_STATUSES = frozenset({"success", "succeeded", "completed"})
_PIPELINE_SKIPPED_STATUSES = frozenset({"skipped"})
_PIPELINE_TERMINAL_STATUSES = (
    _PIPELINE_COMPLETED_STATUSES
    | _PIPELINE_WARNING_STATUSES
    | _PIPELINE_SKIPPED_STATUSES
    | {"manual"}
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _duration(started_at: datetime | None, finished_at: datetime | None) -> float | None:
    if not started_at:
        return None
    finish = finished_at or datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    return max((finish - started_at).total_seconds(), 0)


def _pipeline_job_status(job: PipelineJob) -> str:
    return (job.status or "unknown").strip().lower()


def _pipeline_stage_status(jobs: list[PipelineJob]) -> str:
    """Resolve a stage state without treating missing or unknown data as success."""
    statuses = [_pipeline_job_status(job) for job in jobs]
    if any(status in _PIPELINE_FAILED_STATUSES for status in statuses):
        return "failed"
    if any(status in _PIPELINE_RUNNING_STATUSES for status in statuses):
        return "running"
    if any(status in _PIPELINE_FAILED_STATUSES or status in _PIPELINE_WARNING_STATUSES for status in statuses):
        return "warning"
    if statuses and all(status in _PIPELINE_SKIPPED_STATUSES for status in statuses):
        return "skipped"
    if statuses and all(status in _PIPELINE_COMPLETED_STATUSES for status in statuses):
        return "completed"
    if statuses and all(status in _PIPELINE_TERMINAL_STATUSES for status in statuses):
        return "completed"
    if any(status in _PIPELINE_WAITING_STATUSES for status in statuses):
        return "waiting"
    return "waiting"


def _authoritative_pipeline_jobs(jobs: list[PipelineJob]) -> list[PipelineJob]:
    """Keep only the newest retained GitLab attempt for each job name."""
    current: dict[str, PipelineJob] = {}
    for job in jobs:
        key = (job.name or f"job-{job.gitlab_job_id}").strip().lower()
        previous = current.get(key)
        if previous is None or (job.gitlab_job_id or 0) > (previous.gitlab_job_id or 0):
            current[key] = job
    return list(current.values())


def _pipeline_job_payload(job: PipelineJob) -> dict[str, Any]:
    status = _pipeline_job_status(job)
    return {
        "id": job.id,
        "gitlab_job_id": job.gitlab_job_id,
        "name": job.name,
        "stage": job.stage,
        "status": status,
        "ref": job.ref,
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "duration_seconds": job.duration_seconds,
        "error": None,
        "allow_failure": None,
        "retry": None,
        "last_seen_at": _iso(job.last_seen_at),
        "links": {"gitlab": job.web_url},
    }


def _product_map(db: Session) -> dict[int, Product]:
    return {item.id: item for item in db.query(Product).all()}


def _repository_map(db: Session) -> dict[int, Repository]:
    return {
        item.id: item
        for item in db.query(Repository)
        .options(joinedload(Repository.product))
        .all()
    }


def _median(values: list[float]) -> float | None:
    clean = sorted(float(value) for value in values)
    if not clean:
        return None
    midpoint = len(clean) // 2
    if len(clean) % 2:
        return clean[midpoint]
    return (clean[midpoint - 1] + clean[midpoint]) / 2


def _ptai_scan_is_active(scan: PtaiScan) -> bool:
    """One predicate shared by the PT AI summary and every returned row."""
    status = (scan.asoc_status or "").upper()
    stage = (scan.ptai_stage or "").lower()
    return bool(
        scan.finished_at is None
        and scan.trigger_failed is False
        and status not in _PTAI_TERMINAL_ASOC_STATUSES
        and stage not in _PTAI_TERMINAL_ENGINE_STAGES
    )


def _ptai_active_filter():
    return and_(
        PtaiScan.finished_at.is_(None),
        PtaiScan.trigger_failed.is_(False),
        or_(
            PtaiScan.asoc_status.is_(None),
            func.upper(PtaiScan.asoc_status).notin_(
                tuple(_PTAI_TERMINAL_ASOC_STATUSES)
            ),
        ),
        or_(
            PtaiScan.ptai_stage.is_(None),
            func.lower(PtaiScan.ptai_stage).notin_(
                tuple(_PTAI_TERMINAL_ENGINE_STAGES)
            ),
        ),
    )


def _worker_freshness(
    last_beat_at: datetime | None,
    *,
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, Any]:
    if last_beat_at is None:
        age_seconds = None
    else:
        if last_beat_at.tzinfo is None:
            last_beat_at = last_beat_at.replace(tzinfo=timezone.utc)
        age_seconds = max(int((now - last_beat_at).total_seconds()), 0)
    stale = age_seconds is None or age_seconds > stale_after_seconds
    return {
        "status": "stale" if stale else "healthy",
        "age_seconds": age_seconds,
        "stale": stale,
    }


def _iac_run_summary(
    repositories: int,
    severity_values: list[str],
) -> dict[str, int]:
    return _iac_run_summary_from_counts(
        repositories,
        Counter(
            value.lower() if value else "info"
            for value in severity_values
        ).items(),
    )


def _iac_run_summary_from_counts(
    repositories: int,
    severity_counts: Any,
) -> dict[str, int]:
    known_severities = ("critical", "high", "medium", "low", "info")
    severity: Counter[str] = Counter()
    for value, count in severity_counts:
        normalized = value.lower() if value else "info"
        severity[
            normalized if normalized in known_severities else "info"
        ] += int(count or 0)
    for known_severity in known_severities:
        severity.setdefault(known_severity, 0)
    return {
        "repositories": repositories,
        "findings": int(sum(severity.values())),
        "critical": int(severity["critical"]),
        "high": int(severity["high"]),
        "medium": int(severity["medium"]),
        "low": int(severity["low"]),
        "info": int(severity["info"]),
    }


def _scanner_fleet_summary(
    instances: list[ScannerInstance],
    *,
    total: int,
) -> dict[str, Any]:
    healthy = sum(
        (instance.health_status or "").lower() in {"healthy", "success"}
        for instance in instances
    )
    return {
        "source": "scanner_instances",
        "total": total,
        "returned": len(instances),
        "healthy": healthy,
    }


def _config_finding_payload(item: ConfigScanFinding | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "run_id": item.run_id,
        "project_path": item.project_path,
        "repository_id": item.repository_id,
        "severity": item.severity.lower(),
        "rule_id": item.rule_id,
        "title": item.title,
        "description": item.description,
        "recommendation": item.recommendation,
        "href": f"/config-scan/runs/{item.run_id}/findings",
    }


def _signal_control_direction(direction: str) -> str:
    return "both" if direction == "bidirectional" else direction


def _config_baseline_summary(db: Session) -> dict[str, int]:
    """Match Overview's latest successful per-product Config Guard snapshot."""
    latest_successful_run = (
        db.query(
            ConfigScanRun.product_id,
            func.max(ConfigScanRun.id).label("run_id"),
        )
        .filter(ConfigScanRun.status == "succeeded")
        .group_by(ConfigScanRun.product_id)
        .subquery()
    )
    rows = (
        db.query(ConfigScanRun.scanned_projects, ConfigScanRun.findings_count)
        .join(
            latest_successful_run,
            ConfigScanRun.id == latest_successful_run.c.run_id,
        )
        .all()
    )
    return {
        "products_scanned": sum(
            1 for scanned_projects, _ in rows if (scanned_projects or 0) > 0
        ),
        "open_findings": sum(
            int(findings_count or 0) for _, findings_count in rows
        ),
    }


@router.get("/api/ui/executions", response_class=JSONResponse)
def signal_control_executions(
    status: str = Query("all", max_length=32),
    repository: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None),
    limit: int | None = Query(None, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if page_size is not None and page_size not in (25, 50, 100):
        raise HTTPException(
            status_code=422,
            detail="page_size must be one of 25, 50, or 100",
        )
    effective_page_size = page_size if page_size is not None else limit or 25
    query = db.query(Execution)
    if repository is not None:
        query = query.filter(Execution.repository_id == repository)
    if status != "all":
        query = query.filter(Execution.status == status)
    matching = int(
        query.order_by(None).with_entities(func.count(Execution.id)).scalar()
        or 0
    )
    total_pages = max(1, math.ceil(matching / effective_page_size))
    effective_page = min(page, total_pages)
    executions = (
        query.options(
            joinedload(Execution.steps),
            joinedload(Execution.artifacts).joinedload(Artifact.deliveries),
        )
        .order_by(
            func.coalesce(
                Execution.started_at,
                Execution.requested_at,
                Execution.created_at,
            ).desc(),
            Execution.id.desc(),
        )
        .offset((effective_page - 1) * effective_page_size)
        .limit(effective_page_size)
        .all()
    )
    repositories = _repository_map(db)
    products = _product_map(db)
    status_count_query = db.query(
        Execution.status, func.count(Execution.id)
    )
    if repository is not None:
        status_count_query = status_count_query.filter(
            Execution.repository_id == repository
        )
    status_counts = dict(status_count_query.group_by(Execution.status).all())
    rows = []
    for execution in executions:
        repository_row = repositories.get(execution.repository_id)
        product = products.get(execution.product_id)
        rows.append(
            {
                "id": execution.id,
                "reference": f"EX-{execution.id:05d}",
                "source": execution.source,
                "source_label": Target360Service.source_label(execution.source),
                "status": execution.status,
                "failure_category": execution.failure_category,
                "failure_reason": execution.failure_reason,
                "repository_id": execution.repository_id,
                "repository": repository_row.path if repository_row else None,
                "product_id": execution.product_id,
                "product": product.name if product else None,
                "correlation_key": execution.correlation_key,
                "requested_at": _iso(execution.requested_at),
                "started_at": _iso(execution.started_at),
                "finished_at": _iso(execution.finished_at),
                "duration_seconds": _duration(execution.started_at, execution.finished_at),
                "context": _json(execution.context_json, {}),
                "steps": [
                    {
                        "id": step.id,
                        "name": step.name,
                        "source": step.source,
                        "status": step.status,
                        "failure_category": step.failure_category,
                        "failure_reason": step.failure_reason,
                        "started_at": _iso(step.started_at),
                        "finished_at": _iso(step.finished_at),
                        "duration_seconds": _duration(step.started_at, step.finished_at),
                    }
                    for step in sorted(execution.steps, key=lambda item: item.id)
                ],
                "artifacts": [
                    {
                        "id": artifact.id,
                        "kind": artifact.kind,
                        "status": artifact.status,
                        "size_bytes": artifact.size_bytes,
                        "schema_version": artifact.schema_version,
                        "deliveries": [
                            {
                                "id": delivery.id,
                                "destination": delivery.destination,
                                "status": delivery.status,
                                "failure_category": delivery.failure_category,
                                "failure_reason": delivery.failure_reason,
                                "error_text": delivery.error_text,
                                "attempted_at": _iso(delivery.attempted_at),
                                "confirmed_at": _iso(delivery.confirmed_at),
                            }
                            for delivery in artifact.deliveries
                        ],
                    }
                    for artifact in execution.artifacts
                ],
                "href": (
                    f"/targets/repositories/{execution.repository_id}"
                    if execution.repository_id
                    else "/logs"
                ),
            }
        )
    return {
        "rows": rows,
        "counts": {
            "all": int(sum(status_counts.values())),
            **{key: int(value) for key, value in status_counts.items()},
        },
        "filter": status,
        "repository_filter": repository,
        "matching": matching,
        "pagination": {
            "page": effective_page,
            "page_size": effective_page_size,
            "total_pages": total_pages,
            "start": (
                (effective_page - 1) * effective_page_size + 1
                if matching
                else 0
            ),
            "end": min(effective_page * effective_page_size, matching),
        },
    }


@router.get("/api/ui/failed-jobs", response_class=JSONResponse)
def signal_control_failed_jobs(
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    service = DatabaseService(db)
    jobs = service.get_failed_jobs(limit=limit)
    now = datetime.now(timezone.utc)
    queue_query = service._active_failed_jobs_query()
    open_count, oldest_finished_at = (
        queue_query.with_entities(
            func.count(JobReport.id),
            func.min(JobReport.finished_at),
        )
        .one()
    )
    if db.get_bind().dialect.name == "postgresql":
        median_duration = (
            queue_query.filter(JobReport.duration_seconds.isnot(None))
            .with_entities(
                func.percentile_cont(0.5).within_group(
                    JobReport.duration_seconds
                )
            )
            .scalar()
        )
        median_duration = (
            float(median_duration)
            if median_duration is not None
            else None
        )
    else:
        # SQLite is used by focused unit tests and does not implement
        # percentile_cont; production PostgreSQL stays aggregate-only.
        median_duration = _median(
            [
                float(value)
                for value, in queue_query.filter(
                    JobReport.duration_seconds.isnot(None)
                )
                .with_entities(JobReport.duration_seconds)
                .all()
            ]
        )
    rows = []
    for job in jobs:
        repository = job.repository
        product = repository.product if repository else None
        finished_at = job.finished_at
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        rows.append(
            {
                "id": job.id,
                "reference": f"JOB-{job.id:05d}",
                "name": job.job_name,
                "status": job.job_status.label,
                "repository_id": job.repository_id,
                "repository": repository.path if repository else "Unknown repository",
                "product": product.name if product else None,
                "ref": job.ref,
                "pipeline_url": safe_external_http_url(job.pipeline_url),
                "duration_seconds": job.duration_seconds,
                "finished_at": _iso(job.finished_at),
                "age_seconds": max(int((now - finished_at).total_seconds()), 0),
                "reason": "terminal_job_failed",
                "detail": f"{job.job_name} finished with a failed terminal status.",
                "href": f"/targets/repositories/{job.repository_id}",
            }
        )
    if oldest_finished_at is not None and oldest_finished_at.tzinfo is None:
        oldest_finished_at = oldest_finished_at.replace(tzinfo=timezone.utc)
    return {
        "rows": rows,
        "summary": {
            "open": int(open_count or 0),
            "returned": len(rows),
            "median_duration_seconds": median_duration,
            "oldest_age_seconds": (
                max(int((now - oldest_finished_at).total_seconds()), 0)
                if oldest_finished_at is not None
                else None
            ),
        },
    }


@router.get("/api/ui/job-journal", response_class=JSONResponse)
def signal_control_job_journal(
    repo: str = Query("", max_length=240),
    job: str = Query("", max_length=240),
    category: str = Query("all", max_length=32),
    status: str = Query("all", max_length=32),
    timeframe: str = Query("7d", max_length=16),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Expose the complete job journal contract to the Signal Control UI."""
    allowed_categories = {
        "all",
        "sast",
        "sca",
        "secrets",
        "cs",
        "asoc",
        "mobsf",
    }
    allowed_statuses = {"all", "success", "failed"}
    allowed_timeframes = {"today", "3d", "7d", "30d", "1y", "all"}
    normalized_status = status.lower() if status.lower() in allowed_statuses else "all"
    normalized_timeframe = (
        timeframe.lower()
        if timeframe.lower() in allowed_timeframes
        else "7d"
    )
    normalized_category = (
        category.lower()
        if category and category.lower() in allowed_categories
        else "all"
    )
    category_filter = None if normalized_category == "all" else normalized_category
    only_failed = normalized_status == "failed"

    service = DatabaseService(db)
    jobs, total = service.get_jobs_paginated(
        repo_filter=repo.strip() or None,
        job_filter=job.strip() or None,
        category_filter=category_filter,
        status_filter=normalized_status,
        page=page,
        page_size=page_size,
        only_failed=only_failed,
        timeframe=normalized_timeframe,
    )
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages
        jobs, total = service.get_jobs_paginated(
            repo_filter=repo.strip() or None,
            job_filter=job.strip() or None,
            category_filter=category_filter,
            status_filter=normalized_status,
            page=page,
            page_size=page_size,
            only_failed=only_failed,
            timeframe=normalized_timeframe,
        )

    query = service.get_jobs_query(
        repo_filter=repo.strip() or None,
        job_filter=job.strip() or None,
        category_filter=category_filter,
        status_filter=normalized_status,
        only_failed=only_failed,
        timeframe=normalized_timeframe,
    )
    duration_query = query.filter(JobReport.duration_seconds.isnot(None))
    duration_samples = duration_query.count()
    if db.get_bind().dialect.name == "postgresql":
        median_duration = (
            duration_query
            .with_entities(
                func.percentile_cont(0.5).within_group(
                    JobReport.duration_seconds
                )
            )
            .scalar()
        )
        median_duration = (
            float(median_duration)
            if median_duration is not None
            else None
        )
    else:
        median_duration = _median(
            [
                float(value)
                for value, in duration_query.with_entities(
                    JobReport.duration_seconds
                ).all()
            ]
        )
    oldest_finished_at = query.with_entities(func.min(JobReport.finished_at)).scalar()
    outcome_counts = {
        status: int(count)
        for status, count in query.filter(
            JobReport.job_status.in_([JobStatus.SUCCESS, JobStatus.FAILED])
        )
        .with_entities(JobReport.job_status, func.count(JobReport.id))
        .group_by(JobReport.job_status)
        .all()
    }
    successful_jobs = outcome_counts.get(JobStatus.SUCCESS, 0)
    failed_jobs = outcome_counts.get(JobStatus.FAILED, 0)
    terminal_jobs = successful_jobs + failed_jobs
    now = datetime.now(timezone.utc)
    rows = []
    for record in jobs:
        repository = record.repository
        product = repository.product if repository else None
        status_label = (
            record.job_status.label if record.job_status else "unknown"
        )
        is_terminal = status_label.lower() in {
            "success",
            "failed",
            "cancelled",
            "canceled",
            "skipped",
        }
        rows.append(
            {
                "id": record.id,
                "reference": f"JOB-{record.id:05d}",
                "name": record.job_name,
                "status": status_label,
                "repository_id": record.repository_id,
                "repository": repository.path if repository else "Unknown repository",
                "product": product.name if product else None,
                "ref": record.ref,
                "pipeline_url": safe_external_http_url(record.pipeline_url),
                "duration_seconds": record.duration_seconds,
                "finished_at": _iso(record.finished_at),
                "detail": (
                    f"{record.job_name} finished with a {status_label} terminal status."
                    if is_terminal
                    else f"{record.job_name} last reported with status {status_label}."
                ),
                "href": (
                    f"/targets/repositories/{record.repository_id}"
                    if record.repository_id
                    else "/logs"
                ),
            }
        )
    if oldest_finished_at is not None and oldest_finished_at.tzinfo is None:
        oldest_finished_at = oldest_finished_at.replace(tzinfo=timezone.utc)
    repo_options, job_options = service.get_job_filters(
        category_filter=category_filter,
        only_failed=only_failed,
        timeframe=normalized_timeframe,
    )
    return {
        "rows": rows,
        "summary": {
            "matching": int(total),
            "returned": len(rows),
            "successful": successful_jobs,
            "failed": failed_jobs,
            "terminal": terminal_jobs,
            "success_rate": (
                round((successful_jobs / terminal_jobs) * 100, 1)
                if terminal_jobs
                else None
            ),
            "duration_samples": int(duration_samples),
            "median_duration_seconds": median_duration,
            "oldest_finished_at": _iso(oldest_finished_at),
            "oldest_age_seconds": (
                max(int((now - oldest_finished_at).total_seconds()), 0)
                if oldest_finished_at is not None
                else None
            ),
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "start": (page - 1) * page_size + 1 if total else 0,
            "end": min(page * page_size, total),
        },
        "filters": {
            "repo": repo,
            "job": job,
            "category": normalized_category,
            "status": normalized_status,
            "timeframe": normalized_timeframe,
            "repositories": repo_options,
            "jobs": job_options,
        },
    }


@router.get("/api/ui/pipelines", response_class=JSONResponse)
def signal_control_pipelines(
    group: str | None = Query(None, max_length=32),
    timeframe: str = Query("7d", max_length=8),
    status: str | None = Query(None, max_length=32),
    product: str | None = Query(None, max_length=32),
    repository: str | None = Query(None, max_length=32),
    page: str | None = Query(None, max_length=16),
    page_size: str | None = Query(None, max_length=16),
    db: Session = Depends(get_db),
):
    filters = normalize_pipeline_filters(
        group=group,
        timeframe=timeframe,
        status=status,
        product=product,
        repository=repository,
        page=page,
        page_size=page_size,
    )
    service = PipelineQueryService(db)
    result = service.fetch(filters)
    filter_options = service.filter_options()
    rows = []
    for row in result.rows:
        pipeline = row.pipeline
        rows.append(
            {
                "id": pipeline.id,
                "pipeline_id": pipeline.pipeline_id,
                "status": _enum(pipeline.status),
                "repository_id": pipeline.repository_id,
                "repository": row.repository.path if row.repository else None,
                "product_id": pipeline.product_id,
                "product": row.product.name if row.product else None,
                "ref": pipeline.git_ref,
                "commit_sha": pipeline.commit_sha,
                "job_count": row.job_count,
                "activity_at": _iso(row.activity_at),
                "started_at": _iso(pipeline.started_at),
                "finished_at": _iso(pipeline.finished_at),
                "duration_seconds": pipeline.duration_seconds,
                "error_message": pipeline.error_message,
                "web_url": pipeline.web_url,
                "href": (
                    f"/targets/repositories/{pipeline.repository_id}"
                    if pipeline.repository_id
                    else "/pipelines"
                ),
            }
        )
    return {
        "rows": rows,
        "counts": {
            key.value: int(value) for key, value in result.group_counts.items()
        },
        "total": result.total_count,
        "matching": result.matching_count,
        "page": result.filters.page,
        "page_size": result.filters.page_size,
        "page_count": result.page_count,
        "group": result.filters.group.value,
        "timeframe": result.filters.timeframe,
        "status": _enum(result.filters.status),
        "product": result.filters.product_id,
        "repository": result.filters.repository_id,
        "filter_options": {
            "products": [
                {
                    "id": option.id,
                    "name": option.name,
                    "path": option.path,
                }
                for option in filter_options.products
            ],
            "repositories": [
                {
                    "id": option.id,
                    "path": option.path,
                    "product_id": option.product_id,
                }
                for option in filter_options.repositories
            ],
        },
    }


@router.get("/api/ui/pipelines/{active_pipeline_id}", response_class=JSONResponse)
def signal_control_pipeline_detail(
    active_pipeline_id: int,
    db: Session = Depends(get_db),
):
    """Return the persisted execution river for one internal pipeline record."""
    pipeline = (
        db.query(ActivePipeline)
        .options(
            joinedload(ActivePipeline.repository).joinedload(Repository.product),
            joinedload(ActivePipeline.product),
            joinedload(ActivePipeline.jobs),
        )
        .filter(ActivePipeline.id == active_pipeline_id)
        .first()
    )
    if pipeline is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    tracked_jobs = pipeline.tracked_jobs
    tracked_only = isinstance(tracked_jobs, list) and bool(tracked_jobs)
    retained_jobs = list(pipeline.jobs)
    if tracked_only:
        tracked_names = {
            str(name).strip().lower()
            for name in tracked_jobs
            if str(name).strip()
        }
        retained_jobs = [
            job for job in retained_jobs
            if (job.name or "").strip().lower() in tracked_names
        ]

    jobs = sorted(
        _authoritative_pipeline_jobs(retained_jobs),
        key=lambda job: (
            (job.stage or "Unstaged").strip().lower(),
            (job.name or "").lower(),
            job.id or 0,
            job.gitlab_job_id or 0,
        ),
    )
    stage_jobs: dict[str, list[PipelineJob]] = {}
    for job in jobs:
        stage = (job.stage or "Unstaged").strip() or "Unstaged"
        stage_jobs.setdefault(stage, []).append(job)

    stages = []
    for order, stage_name in enumerate(sorted(stage_jobs), start=1):
        stage_rows = stage_jobs[stage_name]
        succeeded = sum(
            _pipeline_job_status(job) in _PIPELINE_COMPLETED_STATUSES
            for job in stage_rows
        )
        stages.append(
            {
                "id": f"stage-{order}",
                "name": stage_name,
                "status": _pipeline_stage_status(stage_rows),
                "succeeded": succeeded,
                "total": len(stage_rows),
                "jobs": [_pipeline_job_payload(job) for job in stage_rows],
            }
        )

    if tracked_only:
        completeness = {
            "mode": "tracked_only",
            "is_partial": True,
            "jobs_complete": False,
            "known_jobs": len(jobs),
            "expected_jobs": None,
            "tracked_job_names": tracked_jobs,
            "message": "Only jobs named by the registration contract are persisted; other GitLab jobs may be absent.",
        }
    else:
        completeness = {
            "mode": "observed",
            "is_partial": None,
            "jobs_complete": None,
            "known_jobs": len(jobs),
            "expected_jobs": None,
            "tracked_job_names": None,
            "message": "This view contains every job currently persisted by the tracker; completeness cannot be verified from retained data.",
        }

    status = _enum(pipeline.status)
    active = status in {"registered", "polling"}
    succeeded_jobs = sum(
        _pipeline_job_status(job) in _PIPELINE_COMPLETED_STATUSES for job in jobs
    )
    failed_jobs = sum(
        _pipeline_job_status(job) in _PIPELINE_FAILED_STATUSES for job in jobs
    )
    skipped_jobs = sum(
        _pipeline_job_status(job) in _PIPELINE_SKIPPED_STATUSES for job in jobs
    )
    repository = pipeline.repository
    product = pipeline.product or (repository.product if repository else None)
    repository_url = (
        f"{product.gitlab_base_url.rstrip('/')}/{repository.path.strip('/') }"
        if product and repository and product.gitlab_base_url and repository.path
        else None
    )
    return {
        "pipeline": {
            "id": pipeline.id,
            "pipeline_id": pipeline.pipeline_id,
            "project_id": pipeline.project_id,
            "repository_id": pipeline.repository_id,
            "repository": repository.path if repository else None,
            "product": product.name if product else None,
            "ref": pipeline.git_ref,
            "commit_sha": pipeline.commit_sha,
            "status": status,
            "started_at": _iso(pipeline.started_at),
            "finished_at": _iso(pipeline.finished_at),
            "duration_seconds": pipeline.duration_seconds,
            "error_message": pipeline.error_message,
            "last_polled_at": _iso(pipeline.last_polled_at),
            "sync": {
                "last_polled_at": _iso(pipeline.last_polled_at),
                "mode": "polling" if active else "snapshot",
                "refresh_seconds": 15 if active else None,
            },
        },
        "completeness": completeness,
        "summary": {
            "stages": len(stages),
            "jobs": len(jobs),
            "succeeded_jobs": succeeded_jobs,
            "failed_jobs": failed_jobs,
            "skipped_jobs": skipped_jobs,
            "duration_seconds": pipeline.duration_seconds,
        },
        "ordering": {
            "mode": "alphabetical_name",
            "message": "GitLab does not persist the declared CI stage order in this snapshot; stages are grouped by name for a stable view.",
        },
        "stages": stages,
        "links": {
            "pipelines": "/pipelines",
            "gitlab": pipeline.web_url,
            "repository": repository_url,
        },
    }


@router.get("/api/ui/config-guard", response_class=JSONResponse)
def signal_control_config_guard(
    page: int = Query(1, ge=1),
    page_size: int = Query(25),
    db: Session = Depends(get_db),
):
    if page_size not in (25, 50, 100):
        raise HTTPException(
            status_code=422,
            detail="page_size must be one of 25, 50, or 100",
        )
    run_order = (ConfigScanRun.created_at.desc(), ConfigScanRun.id.desc())
    runs = (
        db.query(ConfigScanRun)
        .options(joinedload(ConfigScanRun.product))
        .order_by(*run_order)
        .limit(30)
        .all()
    )
    active_run = (
        db.query(ConfigScanRun)
        .options(joinedload(ConfigScanRun.product))
        .filter(ConfigScanRun.status.in_(("queued", "running")))
        .order_by(*run_order)
        .first()
    )
    selected_run = (
        db.query(ConfigScanRun)
        .options(joinedload(ConfigScanRun.product))
        .filter(
            ConfigScanRun.status.in_(("succeeded", "failed", "cancelled"))
        )
        .order_by(*run_order)
        .first()
    )
    if selected_run is None:
        selected_run = active_run
    for run in (active_run, selected_run):
        if run is not None and all(item.id != run.id for item in runs):
            runs.append(run)
    findings_query = db.query(ConfigScanFinding)
    finding_count = int(
        findings_query.order_by(None)
        .with_entities(func.count(ConfigScanFinding.id))
        .scalar()
        or 0
    )
    total_pages = max(1, math.ceil(finding_count / page_size))
    effective_page = min(page, total_pages)
    findings = (
        findings_query
        .options(joinedload(ConfigScanFinding.repository))
        .order_by(
            ConfigScanFinding.created_at.desc(),
            ConfigScanFinding.id.desc(),
        )
        .offset((effective_page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    selected_finding = (
        db.query(ConfigScanFinding)
        .filter(ConfigScanFinding.run_id == selected_run.id)
        .order_by(
            ConfigScanFinding.created_at.desc(),
            ConfigScanFinding.id.desc(),
        )
        .first()
        if selected_run is not None
        else None
    )
    normalized_severity = func.lower(ConfigScanFinding.severity)
    severity = {
        severity_name: int(count)
        for severity_name, count in (
            db.query(
                normalized_severity,
                func.count(ConfigScanFinding.id),
            )
            .group_by(normalized_severity)
            .all()
        )
    }
    baseline = _config_baseline_summary(db)
    active = int(
        db.query(func.count(ConfigScanRun.id))
        .filter(ConfigScanRun.status.in_(("queued", "running")))
        .scalar()
        or 0
    )
    return {
        "active_run_id": active_run.id if active_run else None,
        "selected_run_id": selected_run.id if selected_run else None,
        "selected_finding": _config_finding_payload(selected_finding),
        "summary": {
            **baseline,
            "retained_findings": finding_count,
        },
        "runs": [
            {
                "id": run.id,
                "product_id": run.product_id,
                "product": run.product.name if run.product else None,
                "status": run.status,
                "trigger": run.trigger,
                "scope_path": run.scope_path,
                "total_projects": run.total_projects,
                "scanned_projects": run.scanned_projects,
                "findings_count": run.findings_count,
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "duration_seconds": _duration(run.started_at, run.finished_at),
                "error_text": run.error_text,
                "href": f"/config-scan/runs/{run.id}",
            }
            for run in runs
        ],
        "findings": [_config_finding_payload(item) for item in findings],
        "severity": severity,
        "active": active,
        "pagination": {
            "page": effective_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "start": (
                (effective_page - 1) * page_size + 1
                if finding_count
                else 0
            ),
            "end": min(effective_page * page_size, finding_count),
        },
    }


@router.get("/api/ui/iac", response_class=JSONResponse)
def signal_control_iac(db: Session = Depends(get_db)):
    run_order = (IacScanRun.created_at.desc(), IacScanRun.id.desc())
    runs = (
        db.query(IacScanRun)
        .order_by(*run_order)
        .limit(30)
        .all()
    )
    selected_run = (
        db.query(IacScanRun)
        .filter(IacScanRun.status.in_(tuple(_IAC_EVIDENCE_STATUSES)))
        .order_by(*run_order)
        .first()
    )
    if selected_run is None:
        selected_run = db.query(IacScanRun).order_by(*run_order).first()
    active_run = (
        db.query(IacScanRun)
        .filter(IacScanRun.status.in_(tuple(_IAC_ACTIVE_STATUSES)))
        .order_by(*run_order)
        .first()
    )
    if selected_run is not None and all(
        run.id != selected_run.id for run in runs
    ):
        runs.append(selected_run)
    if active_run is not None and all(run.id != active_run.id for run in runs):
        runs.append(active_run)
    selected_run_id = selected_run.id if selected_run else None
    repositories = (
        db.query(IacScanRepository)
        .filter(IacScanRepository.run_id == selected_run_id)
        .order_by(
            IacScanRepository.updated_at.desc(),
            IacScanRepository.id.desc(),
        )
        .limit(100)
        .all()
        if selected_run_id is not None
        else []
    )
    findings = (
        db.query(IacScanFinding)
        .filter(IacScanFinding.run_id == selected_run_id)
        .order_by(
            case(
                (func.lower(IacScanFinding.severity) == "critical", 0),
                (func.lower(IacScanFinding.severity) == "high", 1),
                (func.lower(IacScanFinding.severity) == "medium", 2),
                (func.lower(IacScanFinding.severity) == "low", 3),
                else_=4,
            ),
            IacScanFinding.created_at.desc(),
            IacScanFinding.id.desc(),
        )
        .limit(100)
        .all()
        if selected_run_id is not None
        else []
    )
    selected_repository_count = (
        int(
            db.query(func.count(IacScanRepository.id))
            .filter(IacScanRepository.run_id == selected_run_id)
            .scalar()
            or 0
        )
        if selected_run_id is not None
        else 0
    )
    selected_severity_rows = (
        db.query(
            func.lower(IacScanFinding.severity),
            func.count(IacScanFinding.id),
        )
        .filter(IacScanFinding.run_id == selected_run_id)
        .group_by(func.lower(IacScanFinding.severity))
        .all()
        if selected_run_id is not None
        else []
    )
    return {
        "active_run_id": active_run.id if active_run else None,
        "latest_run_id": runs[0].id if runs else None,
        "selected_run_id": selected_run_id,
        "summary": (
            _iac_run_summary_from_counts(
                selected_repository_count,
                selected_severity_rows,
            )
            if selected_run_id is not None
            else None
        ),
        "runs": [
            {
                "id": run.id,
                "status": run.status,
                "trigger": run.trigger,
                "target": run.target_label,
                "scanner": run.scanner_name,
                "total_repositories": run.total_repositories,
                "candidate_repositories": run.candidate_repositories,
                "scanned_repositories": run.scanned_repositories,
                "uploaded_repositories": run.uploaded_repositories,
                "findings_count": run.findings_count,
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "duration_seconds": _duration(run.started_at, run.finished_at),
                "error_text": run.error_text,
                "href": f"/iac/runs/{run.id}",
            }
            for run in runs
        ],
        "repositories": [
            {
                "id": item.id,
                "run_id": item.run_id,
                "repository_id": item.repository_id,
                "product_id": item.product_id,
                "project_path": item.project_path,
                "project_url": item.project_url,
                "has_iac": item.has_iac,
                "markers": _json(item.iac_markers, []),
                "finder_status": item.finder_status,
                "scan_status": item.scan_status,
                "upload_status": item.asoc_upload_status,
                "findings_count": item.findings_count,
                "critical": item.critical_count,
                "high": item.high_count,
                "medium": item.medium_count,
                "low": item.low_count,
                "error_text": item.error_text,
                "href": f"/iac/repositories/{item.id}",
            }
            for item in repositories
        ],
        "findings": [
            {
                "id": item.id,
                "run_id": item.run_id,
                "project_path": item.project_path,
                "repository_id": item.repository_id,
                "severity": item.severity.lower(),
                "rule_id": item.rule_id,
                "title": item.title,
                "file_path": item.file_path,
                "line": item.line,
                "recommendation": item.recommendation,
                "href": f"/iac/repositories/{item.scan_repository_id}",
            }
            for item in findings
        ],
    }


@router.get("/api/ui/ptai", response_class=JSONResponse)
def signal_control_ptai(
    page: int = Query(1, ge=1),
    page_size: int = Query(25),
    status: str = Query("all", max_length=32),
    repo: str = Query("", max_length=240),
    branch: str = Query("", max_length=240),
    problem: bool = Query(False),
    db: Session = Depends(get_db),
):
    if page_size not in (25, 50, 100):
        raise HTTPException(
            status_code=422,
            detail="page_size must be one of 25, 50, or 100",
        )
    normalized_status = (status or "all").strip().upper()
    normalized_repo = (repo or "").strip()
    normalized_branch = (branch or "").strip()
    scans, matching = DatabaseService(db).list_ptai_scans(
        limit=page_size,
        offset=(page - 1) * page_size,
        status=normalized_status,
        repo=normalized_repo,
        branch=normalized_branch,
        problem_only=problem,
    )
    total_pages = max(1, math.ceil(matching / page_size))
    effective_page = min(page, total_pages)
    if effective_page != page:
        scans, matching = DatabaseService(db).list_ptai_scans(
            limit=page_size,
            offset=(effective_page - 1) * page_size,
            status=normalized_status,
            repo=normalized_repo,
            branch=normalized_branch,
            problem_only=problem,
        )
    total = int(db.query(func.count(PtaiScan.id)).scalar() or 0)
    active = int(
        db.query(func.count(PtaiScan.id))
        .filter(_ptai_active_filter())
        .scalar()
        or 0
    )
    asoc_base_url = runtime_setting("HEXWAY_URL", "", db=db)
    ptai_base_url = runtime_setting("PTAI_URL", "", db=db)
    ptai_template = runtime_setting("PTAI_RESULT_URL_TEMPLATE", "", db=db)
    return {
        "summary": {
            "total": total,
            "returned": len(scans),
            "active": active,
        },
        "matching": matching,
        "pagination": {
            "page": effective_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "start": (effective_page - 1) * page_size + 1 if matching else 0,
            "end": min(effective_page * page_size, matching),
        },
        "filters": {
            "status": normalized_status,
            "repo": normalized_repo,
            "branch": normalized_branch,
            "problem": problem,
        },
        "rows": [
            {
                **{
                    "canonical_status": resolve_ptai_outcome(scan).canonical_status,
                    "failure_source": resolve_ptai_outcome(scan).failure_source,
                    "outcome_reason": resolve_ptai_outcome(scan).reason,
                },
                "id": scan.id,
                "repository_id": scan.repository_id,
                "repository": scan.repository.path if scan.repository else scan.repo_slug,
                "product_id": scan.product_id,
                "product": scan.product.name if scan.product else None,
                "branch": scan.branch,
                "status": scan.asoc_status,
                "trigger_failed": scan.trigger_failed,
                "is_active": _ptai_scan_is_active(scan),
                "ptai_stage": scan.ptai_stage,
                "progress": scan.ptai_progress,
                "has_errors": scan.ptai_has_errors,
                "error": scan.ptai_error or scan.error_text,
                "created_at": _iso(scan.created_at),
                "updated_at": _iso(scan.updated_at),
                "finished_at": _iso(scan.finished_at),
                "job_web_url": scan.job_web_url,
                "asoc_repository_url": asoc_repository_url(scan, asoc_base_url),
                "ptai_result_url": ptai_result_url(scan, ptai_base_url, ptai_template),
                "href": f"/ptai-scans/{scan.id}",
            }
            for scan in scans
        ]
    }


@router.get("/api/ui/coverage", response_class=JSONResponse)
def signal_control_coverage(db: Session = Depends(get_db)):
    audit = AuditService(db)
    summary = audit.get_latest_summary()
    coverage = audit.get_latest_product_coverage()
    products = (
        db.query(Product)
        .options(joinedload(Product.repositories))
        .order_by(Product.name.asc())
        .all()
    )
    repository_ids = [
        repository.id for product in products for repository in product.repositories
    ]
    controls = coverage_by_repository(db, repository_ids)
    rows = []
    for product in products:
        repository_control_rows = [
            controls.get(repository.id, {}) for repository in product.repositories
        ]
        expected = sum(int(item.get("expected", 0)) for item in repository_control_rows)
        configured = sum(int(item.get("configured", 0)) for item in repository_control_rows)
        audit_row = coverage.get(
            product.id, {"covered": 0, "total": 0, "last_scanned_at": None}
        )
        rows.append(
            {
                "product_id": product.id,
                "product": product.name,
                "repositories": len(product.repositories),
                "audit_covered": int(audit_row.get("covered", 0)),
                "audit_total": int(audit_row.get("total", 0)),
                "controls_expected": expected,
                "controls_configured": configured,
                "controls_missing": max(expected - configured, 0),
                "last_scanned_at": _iso(audit_row.get("last_scanned_at")),
                "href": f"/targets/products/{product.id}",
            }
        )
    total_expected = sum(row["controls_expected"] for row in rows)
    total_configured = sum(row["controls_configured"] for row in rows)
    return {
        "summary": {
            "covered": int(summary.get("covered", 0)),
            "pipeline_no_sec": int(summary.get("pipeline_no_sec", 0)),
            "no_pipelines": int(summary.get("no_pipelines", 0)),
            "inaccessible": int(summary.get("inaccessible", 0)),
            "excluded": int(summary.get("excluded", 0)),
            "controls_expected": total_expected,
            "controls_configured": total_configured,
            "control_rate": round(
                total_configured / total_expected * 100, 1
            )
            if total_expected
            else 0,
        },
        "rows": rows,
    }


@router.get("/api/ui/repositories", response_class=JSONResponse)
def signal_control_repositories(
    q: str = Query("", max_length=240),
    product: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None),
    limit: int | None = Query(None, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    if page_size is not None and page_size not in (25, 50, 100):
        raise HTTPException(
            status_code=422,
            detail="page_size must be one of 25, 50, or 100",
        )
    effective_page_size = page_size if page_size is not None else limit or 25
    normalized_q = q.strip()
    total = int(db.query(func.count(Repository.id)).scalar() or 0)
    query = (
        db.query(Repository)
        .outerjoin(Product, Repository.product_id == Product.id)
    )
    if product:
        query = query.filter(Repository.product_id == product)
    if normalized_q:
        search = normalized_q.casefold()
        query = query.filter(
            or_(
                func.lower(Repository.path).contains(search, autoescape=True),
                func.lower(Product.name).contains(search, autoescape=True),
            )
        )
    matching = int(
        query.order_by(None).with_entities(func.count(Repository.id)).scalar()
        or 0
    )
    total_pages = max(1, math.ceil(matching / effective_page_size))
    effective_page = min(page, total_pages)
    repositories = (
        query.options(joinedload(Repository.product))
        .order_by(Repository.path.asc(), Repository.id.asc())
        .offset((effective_page - 1) * effective_page_size)
        .limit(effective_page_size)
        .all()
    )
    service = DatabaseService(db)
    stats_by_repository = service.get_repository_stats_batch(
        [item.id for item in repositories]
    )
    controls = coverage_by_repository(db, [item.id for item in repositories])
    rows = []
    for repository in repositories:
        stats = stats_by_repository.get(repository.id, {})
        coverage = controls.get(
            repository.id,
            {"expected": 0, "configured": 0, "missing": 0, "excluded": 0},
        )
        rows.append(
            {
                "id": repository.id,
                "path": repository.path,
                "product_id": repository.product_id,
                "product": repository.product.name if repository.product else None,
                "status": _enum(repository.overall_status) or "unknown",
                "last_report_at": _iso(repository.last_report_at),
                "jobs_total": stats.get("total_jobs", 0),
                "jobs_success": stats.get("successful_jobs", 0),
                "jobs_failed": stats.get("failed_jobs", 0),
                "success_rate": stats.get("success_rate", 0),
                "job_health_healthy": stats.get("job_health_healthy", 0),
                "job_health_broken": stats.get("job_health_broken", 0),
                "job_health_total": stats.get("job_health_total", 0),
                "coverage": coverage,
                "href": f"/targets/repositories/{repository.id}",
            }
        )
    return {
        "rows": rows,
        "total": total,
        "matching": matching,
        "returned": len(rows),
        "pagination": {
            "page": effective_page,
            "page_size": effective_page_size,
            "total_pages": total_pages,
            "start": (
                (effective_page - 1) * effective_page_size + 1
                if matching
                else 0
            ),
            "end": min(effective_page * effective_page_size, matching),
        },
        "filters": {
            "q": normalized_q,
            "product": product,
        },
        "product_filter": product,
    }


@router.get("/api/ui/repositories/{repository_id}", response_class=JSONResponse)
def signal_control_repository(
    repository_id: int,
    source: str = Query("all", max_length=64),
    db: Session = Depends(get_db),
):
    try:
        snapshot = Target360Service(db).snapshot(
            repository_id, source=None if source == "all" else source
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc
    repository = snapshot["repository"]
    product = snapshot.get("product")
    return {
        "repository": {
            "id": repository.id,
            "path": repository.path,
            "status": _enum(repository.overall_status) or "unknown",
            "last_report_at": _iso(repository.last_report_at),
            "href": f"/targets/repositories/{repository.id}",
        },
        "product": {
            "id": product.id,
            "name": product.name,
            "href": f"/targets/products/{product.id}",
        }
        if product
        else None,
        "coverage": snapshot["coverage"],
        "controls": snapshot["controls"],
        "executions": snapshot["executions"],
        "source_options": snapshot["source_options"],
        "summary": snapshot["summary"],
        "freshness": snapshot["freshness"],
        "versions": snapshot["versions"],
    }


def _record_operator_outcome(db: Session, **outcome: Any) -> None:
    """Record the attempt without letting a state-write error hide its result.

    The sync (or its confirmation prompt) is what the operator needs; the
    last-attempt banner is secondary.
    """
    try:
        record_sync_outcome(db, **outcome)
    except Exception as exc:  # noqa: BLE001 - never replace the sync response
        db.rollback()
        logger.warning("ASOC sync outcome not recorded error_type=%s", type(exc).__name__)


@router.post("/api/ui/asoc-reporting/sync", response_class=JSONResponse)
def signal_control_asoc_sync(
    payload: AsocSyncRequest | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """Refresh the ASOC product-card projection used by all product views."""
    payload = payload or AsocSyncRequest()
    started_at = datetime.now(timezone.utc)
    try:
        result = fetch_and_sync(
            db,
            confirm_mass_removal=payload.confirm_mass_removal,
            expected_active_count=payload.expected_active_count,
            expected_incoming_count=payload.expected_incoming_count,
            expected_registry_version=payload.expected_registry_version,
            confirmed_by="operator",
        )
    except AsocSyncConfirmationRequiredError as exc:
        db.rollback()
        _record_operator_outcome(
            db,
            trigger="operator",
            at=started_at,
            status="needs_confirmation",
            message=(
                f"ASOC returned {exc.incoming_count} products while {exc.active_count} are active; "
                f"{exc.removal_count} would be removed."
            ),
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "confirmation_stale" if exc.stale_confirmation else "confirmation_required",
                "message": (
                    "The ASOC registry changed during confirmation. Review the new counts "
                    "and confirm again."
                    if exc.stale_confirmation
                    else "This snapshot would archive an unusually large part of the ASOC registry."
                ),
                "active_count": exc.active_count,
                "incoming_count": exc.incoming_count,
                "removal_count": exc.removal_count,
                "registry_version": exc.registry_version,
            },
        )
    except AsocSyncInProgressError:
        raise HTTPException(
            status_code=409,
            detail="ASOC product synchronization is already in progress.",
        )
    except Exception as exc:
        correlation_id = uuid.uuid4().hex
        logger.warning(
            "ASOC product sync failed correlation_id=%s error_type=%s",
            correlation_id,
            type(exc).__name__,
        )
        db.rollback()
        _record_operator_outcome(
            db,
            trigger="operator",
            at=started_at,
            status="failed",
            message=f"ASOC synchronization failed (reference {correlation_id}).",
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "ASOC product synchronization failed. Check the ASOC integration "
                f"settings or contact an operator with reference {correlation_id}."
            ),
        ) from exc
    _record_operator_outcome(
        db,
        trigger="operator",
        at=started_at,
        status="ok",
        message=sync_result_message(result),
        result=result,
    )
    sync = asoc_sync_metadata(db, configured=True)
    return {
        "ok": True,
        "result": result,
        "sync": {
            **sync,
            "last_synced": _iso(sync["last_synced"]),
        },
    }


@router.get("/api/ui/scanner-fleet", response_class=JSONResponse)
def signal_control_scanner_fleet(db: Session = Depends(get_db)):
    # Connection tests update the integration test-state tables, while this
    # endpoint renders the persisted connector/scanner projection. Refresh the
    # projection first so Scanner Fleet never trails a successful test until a
    # different registry endpoint happens to be opened.
    refresh_registry(db)
    connectors = (
        db.query(Connector)
        .options(joinedload(Connector.instances))
        .order_by(Connector.name.asc())
        .all()
    )
    instances = db.query(ScannerInstance).order_by(ScannerInstance.name.asc()).all()
    workers = db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.name.asc()).all()
    instance_total = int(db.query(func.count(ScannerInstance.id)).scalar() or 0)
    now = datetime.now(timezone.utc)
    worker_stale_after = max(
        0, int(os.getenv("HEALTH_WORKER_STALE_SECONDS", "900"))
    )
    worker_rows = [
        {
            "name": worker.name,
            "last_beat_at": _iso(worker.last_beat_at),
            **_worker_freshness(
                worker.last_beat_at,
                now=now,
                stale_after_seconds=worker_stale_after,
            ),
        }
        for worker in workers
    ]
    workers_by_name = {row["name"]: row for row in worker_rows}
    instance_rows = []
    for instance in instances:
        status = instance.health_status
        reason = instance.health_reason
        last_seen_at = instance.last_seen_at
        if instance.instance_type == "worker":
            worker_name = (instance.instance_key or "").removeprefix("worker:")
            heartbeat = workers_by_name.get(worker_name)
            if heartbeat:
                status = heartbeat["status"]
                reason = (
                    "Worker heartbeat is stale"
                    if heartbeat["stale"]
                    else "Recent heartbeat received"
                )
                last_seen_at = next(
                    (
                        worker.last_beat_at
                        for worker in workers
                        if worker.name == worker_name
                    ),
                    last_seen_at,
                )
        instance_rows.append(
            {
                "id": instance.id,
                "connector_id": instance.connector_id,
                "key": instance.instance_key,
                "name": instance.name,
                "type": instance.instance_type,
                "endpoint": instance.endpoint,
                "version": instance.version,
                "capabilities": _json(instance.capabilities_json, []),
                "status": status,
                "reason": reason,
                "last_seen_at": _iso(last_seen_at),
            }
        )
    summary = {
        "source": "scanner_instances",
        "total": instance_total,
        "returned": len(instance_rows),
        "healthy": sum(
            str(row["status"] or "").lower() in {"healthy", "success"}
            for row in instance_rows
        ),
    }
    return {
        "summary": summary,
        "connectors": [
            {
                "id": connector.id,
                "key": connector.key,
                "name": connector.name,
                "type": connector.connector_type,
                "endpoint": connector.endpoint,
                "enabled": connector.enabled,
                "status": connector.health_status,
                "reason": connector.health_reason,
                "last_seen_at": _iso(connector.last_seen_at),
                "instances": len(connector.instances),
            }
            for connector in connectors
        ],
        "instances": instance_rows,
        "workers": worker_rows,
    }


@router.get("/api/ui/integrations", response_class=JSONResponse)
def signal_control_integrations(db: Session = Depends(get_db)):
    rows = []
    for key in CONNECTOR_SPECS:
        snapshot = connector_snapshot(db, key)
        instances = []
        for instance in snapshot.get("instances", []):
            last_successful_check = instance.get("last_successful_check")
            instances.append(
                {
                    **{
                        item_key: item_value
                        for item_key, item_value in instance.items()
                        if item_key
                        not in {
                            "token",
                            "password",
                            "secret",
                            "private_key",
                            "last_successful_check",
                        }
                    },
                    "tested_at": _iso(instance.get("tested_at")),
                    "last_success_at": _iso(instance.get("last_success_at")),
                    "last_successful_check": (
                        {
                            "at": _iso(last_successful_check.get("at")),
                            "message": last_successful_check.get("message"),
                        }
                        if last_successful_check
                        else None
                    ),
                }
            )
        if key == "gitlab":
            connection_count = len(instances)
            configured_connections = sum(
                1 for instance in instances if instance["token_configured"]
            )
            disabled_connections = sum(
                1
                for instance in instances
                if not snapshot["enabled"] or not instance["enabled"]
            )
            healthy_connections = sum(
                1
                for instance in instances
                if snapshot["enabled"]
                and instance["enabled"]
                and instance["token_configured"]
                and instance["test_status"] == "healthy"
            )
            failed_connections = sum(
                1
                for instance in instances
                if snapshot["enabled"]
                and instance["enabled"]
                and instance["token_configured"]
                and instance["test_status"] == "failed"
            )
            untested_connections = sum(
                1
                for instance in instances
                if snapshot["enabled"]
                and instance["enabled"]
                and instance["token_configured"]
                and instance["test_status"] not in {"healthy", "failed"}
            )
            active_connections = connection_count - disabled_connections
            if failed_connections:
                health_status = "failed"
            elif active_connections and healthy_connections == active_connections:
                health_status = "healthy"
            elif healthy_connections or untested_connections:
                health_status = "partial"
            elif connection_count and disabled_connections == connection_count:
                health_status = "disabled"
            else:
                health_status = "unknown"
            health_reason = (
                f"{healthy_connections} healthy · {failed_connections} failed"
                f" · {untested_connections} untested"
            )
            tested_values = [
                instance.get("tested_at")
                for instance in snapshot.get("instances", [])
                if instance.get("tested_at")
            ]
            success_values = [
                instance.get("last_success_at")
                for instance in snapshot.get("instances", [])
                if instance.get("last_success_at")
            ]
            projected_tested_at = max(tested_values, default=None)
            projected_last_success_at = max(success_values, default=None)
        else:
            connection_count = 1
            field_configuration = {
                field["name"]: bool(field.get("configured"))
                for field in snapshot.get("fields", [])
            }
            required_fields = {
                "asoc": ("url", "token"),
                "ptai": ("url", "token"),
                "mattermost": ("webhook_url",),
            }.get(key, ())
            configured_connections = int(
                all(field_configuration.get(field, False) for field in required_fields)
                if required_fields
                else bool(snapshot["configured"])
            )
            disabled_connections = int(not snapshot["enabled"])
            healthy_connections = int(
                snapshot["enabled"] and snapshot["test_status"] == "healthy"
            )
            failed_connections = int(
                snapshot["enabled"]
                and bool(
                    snapshot["error"] or snapshot["test_status"] == "failed"
                )
            )
            untested_connections = int(
                snapshot["enabled"]
                and snapshot["configured"]
                and not healthy_connections
                and not failed_connections
            )
            health_status = (
                "disabled"
                if disabled_connections
                else "failed"
                if failed_connections
                else "healthy"
                if healthy_connections
                else "partial"
                if untested_connections
                else "unknown"
            )
            health_reason = (
                snapshot["error"]
                or snapshot["test_message"]
                or snapshot["health_label"]
            )
            projected_tested_at = snapshot["tested_at"]
            projected_last_success_at = (
                snapshot.get("last_successful_check") or {}
            ).get("at")
        last_successful_check = snapshot.get("last_successful_check")
        rows.append(
            {
                "key": snapshot["key"],
                "name": snapshot["name"],
                "role": snapshot["role"],
                "detail": snapshot["detail"],
                "direction": _signal_control_direction(snapshot["direction"]),
                "canonical_direction": snapshot["direction"],
                "configured": snapshot["configured"],
                "enabled": snapshot["enabled"],
                "source": snapshot["source"],
                "credential_ref": snapshot["credential_ref"],
                "updated_at": _iso(snapshot["updated_at"]),
                "updated_by": snapshot["updated_by"],
                "tested_at": _iso(projected_tested_at),
                "test_status": snapshot["test_status"],
                "test_message": snapshot["test_message"],
                "health_status": health_status,
                "health_reason": health_reason,
                "connection_summary": {
                    "total": connection_count,
                    "configured": configured_connections,
                    "healthy": healthy_connections,
                    "failed": failed_connections,
                    "untested": untested_connections,
                    "disabled": disabled_connections,
                },
                "last_successful_check": (
                    {
                        "at": _iso(projected_last_success_at),
                        "message": (
                            last_successful_check.get("message")
                            if last_successful_check
                            else None
                        ),
                    }
                    if projected_last_success_at
                    else None
                ),
                "error": snapshot["error"],
                "fields": [
                    {
                        "name": field["name"],
                        "label": field["label"],
                        "kind": field.get("kind", "text"),
                        "secret": bool(field.get("secret")),
                        "value": "" if field.get("secret") else field.get("value", ""),
                        "configured": bool(field.get("configured")),
                    }
                    for field in snapshot.get("fields", [])
                    if field.get("kind") != "instances"
                ],
                "instances": instances,
                "href": f"/integrations?integration={snapshot['key']}",
            }
        )
    auth_ready = bool(
        (os.getenv("UI_AUTH_USERNAME") or "").strip()
        and (os.getenv("UI_AUTH_PASSWORD") or "").strip()
    )
    store_ready = credential_store_ready()
    summary = {
        "integration_types": len(rows),
        "connections": sum(
            row["connection_summary"]["total"] for row in rows
        ),
        "configured": sum(
            row["connection_summary"]["configured"] for row in rows
        ),
        "healthy": sum(
            row["connection_summary"]["healthy"] for row in rows
        ),
        "failed": sum(
            row["connection_summary"]["failed"] for row in rows
        ),
        "untested": sum(
            row["connection_summary"]["untested"] for row in rows
        ),
        "disabled": sum(
            row["connection_summary"]["disabled"] for row in rows
        ),
    }
    return {
        "management": {
            "available": auth_ready and store_ready,
            "auth_ready": auth_ready,
            "credential_store_ready": store_ready,
        },
        "summary": summary,
        "rows": rows,
    }
