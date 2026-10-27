from fastapi import APIRouter, Depends, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from typing import Optional, Iterable, Set, List, Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
import asyncio
import json
import logging
from ...models import (
    SessionLocal,
    Product,
    Repository,
    JobReport,
    ActivePipeline,
    PipelineJob,
    AuditCoverageStatus,
    AuditScanRun,
    ConfigScanRun,
)
from ...database import DatabaseService
from ...audit_service import AuditService
from ...config_scan_service import ConfigScanService, project_counts_for_run
from ...config_scan_reporting import findings_severity_counts, format_scan_duration, gitlab_path_from_product_field
from ...iac_service import IacService
from ...target_360_service import Target360Service
from ...url_safety import safe_evidence_http_url
from ...models.iac import IacScanRun
from ...models.job_report import JobStatus
from ...target_navigation import (
    REPOSITORY_TARGET_PATH,
    compatibility_redirect_url,
    product_target_url,
    repository_target_url,
    targets_url,
)
import math

from fastapi import APIRouter
from .common import (
    _format_duration_summary,
    _normalize_timeframe,
    get_db,
    templates,
    LOG_TIMEFRAME_OPTIONS,
    LOG_TIMEFRAME_VALUES,
)

router = APIRouter(tags=["web"])
logger = logging.getLogger(__name__)
_SIGNAL_CONTROL_INDEX = Path("app/static/next/index.html")

@router.get(
    REPOSITORY_TARGET_PATH,
    response_class=HTMLResponse,
    name="repository_target_360",
)
async def repository_target_360(
    request: Request,
    repository_id: int,
    source: str = Query("all", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
    db: Session = Depends(get_db),
):
    try:
        snapshot = Target360Service(db).snapshot(
            repository_id,
            source=None if source == "all" else source,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc
    product = snapshot.get("product")
    breadcrumbs = [
        {"label": "Security posture"},
        {"label": "Targets", "href": targets_url()},
    ]
    if product:
        breadcrumbs.append(
            {"label": product.name, "href": product_target_url(product.id)}
        )
    breadcrumbs.append(
        {"label": snapshot["repository"].path, "current": True}
    )
    return templates.TemplateResponse(
        "repository_360.html",
        {
            "request": request,
            "selected_source": source,
            "breadcrumbs": breadcrumbs,
            **snapshot,
        },
    )


@router.get("/repos/{repository_id}", response_class=RedirectResponse)
async def repository_target_compatibility(request: Request, repository_id: int):
    return RedirectResponse(
        compatibility_redirect_url(request, repository_target_url(repository_id)),
        status_code=308,
    )


@router.get("/repos/{repository_id}/jobs", response_class=HTMLResponse)
async def repository_logs(
    request: Request,
    repository_id: int,
    job: str = "",
    category: str = "all",
    status: str = "all",
    only_failed: bool = False,
    page: int = 1,
    timeframe: str = "all",
    db: Session = Depends(get_db)
):
    db_service = DatabaseService(db)
    timeframe_value = _normalize_timeframe(timeframe, LOG_TIMEFRAME_VALUES, "all")

    repository = db.query(Repository).filter(Repository.id == repository_id).first()
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    repo_path = repository.path
    repo_name = f"{repository.product.name} / {repository.path}" if repository.product else repository.path

    return _render_jobs_page(
        request,
        db_service,
        repo=repo_path,
        repository_id=repository_id,
        job=job,
        category=category,
        status=status,
        page=page,
        only_failed=only_failed,
        base_path_override=f"/repos/{repository_id}/jobs",
        repo_locked=True,
        repo_display_name=repo_name,
        view_title_override=(
            f"{repository.path} Unresolved Job Failures"
            if only_failed else f"{repository.path} Logs"
        ),
        view_description_override=(
            "Unresolved failed job/ref keys for this repository"
            if only_failed else "Full job history for this repository"
        ),
        empty_title_override="No Unresolved Job Failures" if only_failed else "No Jobs Found",
        empty_text_override=(
            "No unresolved failed jobs match the current filters for this repository."
            if only_failed else "No jobs match the current filters for this repository."
        ),
        duration_summary_override=_format_duration_summary(
            db_service.get_pipeline_duration_summary(repository_id=repository_id, timeframe=timeframe_value)
        ),
        job_duration_summary_override=_format_duration_summary(
            db_service.get_job_duration_summary(
                repository_id=repository_id,
                category_filter=None if category == "all" else category,
                timeframe=timeframe_value,
            )
        ),
        timeframe=timeframe_value,
        breadcrumbs=[
            {"label": "Operations"},
            {"label": "Executions", "href": "/logs"},
            {"label": repository.path, "current": True},
        ],
    )

def _render_jobs_page(
    request: Request,
    db_service: DatabaseService,
    *,
    repo: str,
    job: str,
    category: str,
    status: str = "all",
    page: int,
    only_failed: bool,
    repository_id: Optional[int] = None,
    base_path_override: Optional[str] = None,
    repo_locked: bool = False,
    repo_display_name: Optional[str] = None,
    view_title_override: Optional[str] = None,
    view_description_override: Optional[str] = None,
    empty_title_override: Optional[str] = None,
    empty_text_override: Optional[str] = None,
    duration_summary_override: Optional[dict] = None,
    job_duration_summary_override: Optional[dict] = None,
    timeframe: str = "7d",
    breadcrumbs: Optional[list[dict]] = None,
):
    page_size = 20
    page = max(page, 1)
    category = (category or "all").lower()
    category_filter = None if category == "all" else category

    jobs, total_count = db_service.get_jobs_paginated(
        repo_filter=repo.strip() or None,
        job_filter=job.strip() or None,
        category_filter=category_filter,
        status_filter=status.strip() or None,
        page=page,
        page_size=page_size,
        only_failed=only_failed,
        timeframe=timeframe,
        repository_id=repository_id,
    )

    total_pages = max(1, math.ceil(total_count / page_size))
    if page > total_pages:
        page = total_pages
    jobs, total_count = db_service.get_jobs_paginated(
        repo_filter=repo.strip() or None,
        job_filter=job.strip() or None,
        category_filter=category_filter,
        status_filter=status.strip() or None,
        page=page,
        page_size=page_size,
        only_failed=only_failed,
        timeframe=timeframe,
        repository_id=repository_id,
    )

    repo_options, job_options = db_service.get_job_filters(
        category_filter=category_filter,
        only_failed=only_failed,
        timeframe=timeframe
    )

    job_categories = [
        {"value": "all", "label": "All", "icon": "layers"},
        {"value": "sast", "label": "SAST", "icon": "shield"},
        {"value": "sca", "label": "SCA", "icon": "cube"},
        {"value": "secrets", "label": "Secrets", "icon": "key"},
        {"value": "cs", "label": "CS", "icon": "wrench"},
        {"value": "asoc", "label": "ASOC", "icon": "diagram"},
        {"value": "mobsf", "label": "MobSF", "icon": "diagram"},
    ]

    page_start = (page - 1) * page_size + 1 if total_count else 0
    page_end = min(page * page_size, total_count)
    pipeline_urls_by_job_id = {
        job.id: safe_evidence_http_url(job.pipeline_url)
        for job in jobs
    }

    view_mode = "failed" if only_failed else "all"
    default_view_title = "Jobs"
    default_view_description = "Observed security pipeline jobs across monitored repositories"
    default_empty_title = "No Failed Jobs" if only_failed else "No Jobs Found"
    default_empty_text = (
        "Great! There are no failed security jobs at the moment."
        if only_failed
        else "No jobs match the current filters. Try adjusting the criteria."
    )
    meta_label = "failed jobs" if only_failed else "jobs"
    base_path = base_path_override or "/logs"

    return templates.TemplateResponse("logs.html", {
        "request": request,
        "jobs": jobs,
        "pipeline_urls_by_job_id": pipeline_urls_by_job_id,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "page_start": page_start,
        "page_end": page_end,
        "repo_filter": repo,
        "job_filter": job,
        "category": category,
        "job_categories": job_categories,
        "repo_options": repo_options,
        "job_options": job_options,
        "page_size": page_size,
        "view_mode": view_mode,
        "only_failed": only_failed,
        "view_title": view_title_override or default_view_title,
        "view_description": view_description_override or default_view_description,
        "empty_title": empty_title_override or default_empty_title,
        "empty_text": empty_text_override or default_empty_text,
        "meta_label": meta_label,
        "base_path": base_path,
        "repo_locked": repo_locked,
        "repo_display_name": repo_display_name or repo,
        "timeframe": timeframe,
        "timeframe_options": LOG_TIMEFRAME_OPTIONS,
        "status_filter": status,
        "duration_summary": duration_summary_override,
        "job_duration_summary": job_duration_summary_override,
        "breadcrumbs": breadcrumbs,
    })


@router.get("/failed", response_class=RedirectResponse)
async def failed_logs(
    request: Request,
):
    return RedirectResponse(
        compatibility_redirect_url(
            request,
            "/logs",
            forced_query={"status": "failed"},
        ),
        status_code=308,
    )


@router.get("/logs", response_class=HTMLResponse)
async def logs(
    source: str = "all",
):
    source = source if source in {"all", "pipelines", "config-guard", "checkov", "ptai"} else "all"
    source_destinations = {
        "pipelines": "/pipelines",
        "config-guard": "/config-scan",
        "checkov": "/iac",
        "ptai": "/ptai-scans",
    }
    if source in source_destinations:
        return RedirectResponse(url=source_destinations[source], status_code=303)
    return FileResponse(_SIGNAL_CONTROL_INDEX, media_type="text/html")


# ── IaC routes ─────────────────────────────────────────────────────
