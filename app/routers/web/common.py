from ...product_identity import is_nested_product_namespace
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
import inspect
import json
import logging
import os
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
from ...display_time import DISPLAY_TIMEZONE, DISPLAY_TIMEZONE_NAME
from ...audit_service import AuditService
from ...config_scan_service import ConfigScanService, project_counts_for_run
from ...config_scan_reporting import findings_severity_counts, format_scan_duration, gitlab_path_from_product_field
from ...iac_service import IacService
from ...models.iac import IacScanRun
from ...models.job_report import JobStatus
from ...target_navigation import (
    COVERAGE_PATH,
    COVERAGE_RUNS_PATH,
    coverage_run_url,
    product_coverage_url,
    product_target_url,
    repository_target_url,
    targets_url,
)
import math

templates = Jinja2Templates(directory="app/templates")

# Starlette 0.48 moved ``request`` to the first TemplateResponse argument.
# Keep the repository's existing ``TemplateResponse(name, context)`` calls
# working across both the pinned production stack and newer local runtimes.
_template_response = templates.TemplateResponse
if next(iter(inspect.signature(_template_response).parameters)) == "request":
    def _template_response_compat(
        name,
        context=None,
        status_code=200,
        headers=None,
        media_type=None,
        background=None,
    ):
        context = context or {}
        return _template_response(
            request=context.get("request"),
            name=name,
            context=context,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )

    templates.TemplateResponse = _template_response_compat
# Surfaced as a banner so an unprotected dashboard can't be missed.
templates.env.globals["ui_auth_enabled"] = bool(
    os.getenv("UI_AUTH_USERNAME") and os.getenv("UI_AUTH_PASSWORD")
)
logger = logging.getLogger(__name__)

_DISPLAY_TIMEZONE_NAME = DISPLAY_TIMEZONE_NAME
_DISPLAY_TIMEZONE = DISPLAY_TIMEZONE


def _format_local_datetime(
    value: Optional[datetime | str],
    fmt: str = "%Y-%m-%d %H:%M",
    empty: str = "—",
) -> str:
    if value is None:
        return empty
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return empty
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if not isinstance(value, datetime):
        return str(value)
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(_DISPLAY_TIMEZONE).strftime(fmt)


templates.env.filters["localtime"] = _format_local_datetime
templates.env.globals["ui_timezone"] = _DISPLAY_TIMEZONE_NAME
templates.env.globals.update(
    coverage_url=COVERAGE_PATH,
    coverage_runs_url=COVERAGE_RUNS_PATH,
    coverage_run_url=coverage_run_url,
    product_coverage_url=product_coverage_url,
    product_target_url=product_target_url,
    repository_target_url=repository_target_url,
    targets_url=targets_url,
)

def _parse_gitlab_target_url(target_url: str) -> tuple[str, str] | None:
    raw = (target_url or "").strip().rstrip("/")
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    # Allowlist: the worker resolves a GITLAB token for this host and puts it in
    # the clone URL, so an arbitrary host here would leak the token (SSRF).
    from ...gitlab_url import is_known_gitlab_host

    if not is_known_gitlab_host(f"{parsed.scheme}://{parsed.netloc}"):
        return None
    scope_path = (parsed.path or "").strip("/")
    if not scope_path:
        return None
    return f"{parsed.scheme}://{parsed.netloc}", scope_path

def _month_keys(end_year: int, end_month: int, length: int = 12):
    keys = []
    for i in range(length):
        offset = length - 1 - i
        month = end_month - offset
        year = end_year
        while month <= 0:
            month += 12
            year -= 1
        keys.append((year, month))
    return keys

DASHBOARD_TIMEFRAME_OPTIONS = [
    {"value": "today", "label": "Today"},
    {"value": "3d", "label": "Last 3 days"},
    {"value": "7d", "label": "Last 7 days"},
    {"value": "30d", "label": "Last 30 days"},
    {"value": "1y", "label": "Last 12 months"},
]
DASHBOARD_TOOL_OPTIONS = [
    {"value": "all", "label": "All Tools"},
    {"value": "sast", "label": "SAST"},
    {"value": "sca", "label": "SCA"},
    {"value": "secrets", "label": "Secrets"},
    {"value": "cs", "label": "CS"},
    {"value": "asoc", "label": "ASOC"},
    {"value": "mobsf", "label": "MobSF"},
]
LOG_TIMEFRAME_OPTIONS = DASHBOARD_TIMEFRAME_OPTIONS + [
    {"value": "all", "label": "All time"}
]
DASHBOARD_TIMEFRAME_VALUES: Set[str] = {opt["value"] for opt in DASHBOARD_TIMEFRAME_OPTIONS}
DASHBOARD_TOOL_VALUES: Set[str] = {opt["value"] for opt in DASHBOARD_TOOL_OPTIONS}
LOG_TIMEFRAME_VALUES: Set[str] = {opt["value"] for opt in LOG_TIMEFRAME_OPTIONS}
JOB_DURATION_CATEGORY_OPTIONS = [opt for opt in DASHBOARD_TOOL_OPTIONS if opt["value"] != "all"]

def _normalize_timeframe(value: Optional[str], allowed: Iterable[str], default: str) -> str:
    if not value:
        return default
    normalized = value.lower()
    return normalized if normalized in allowed else default

def _timeframe_span_days(value: str) -> int:
    if value == "today":
        return 1
    return {
        "3d": 3,
        "7d": 7,
        "30d": 30,
        "1y": 365,
    }.get(value, 7)

def _start_of_month(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    minutes = total / 60
    if minutes == 0:
        return "0 min"
    if minutes < 1:
        return f"{minutes:.1f} min"
    return f"{round(minutes)} min"


templates.env.filters["duration"] = _format_duration
def _format_duration_summary(summary: dict) -> dict:
    return {
        **summary,
        "avg_label": _format_duration(summary.get("avg")),
        "median_label": _format_duration(summary.get("median")),
        "p95_label": _format_duration(summary.get("p95")),
        "latest_label": _format_duration(summary.get("latest")),
    }


def _pipeline_web_url(pipeline: ActivePipeline, repo: Optional[Repository], product: Optional[Product]) -> Optional[str]:
    if pipeline.web_url:
        return pipeline.web_url
    if not repo or not product or not product.gitlab_base_url:
        return None
    base = product.gitlab_base_url.rstrip("/")
    repo_path = (repo.path or "").strip("/")
    if not repo_path:
        return None
    return f"{base}/{repo_path}/-/pipelines/{pipeline.pipeline_id}"


def _duration_chart_buckets(now: datetime, timeframe_value: str) -> tuple[str, list[datetime], list[str]]:
    if timeframe_value == "1y":
        month_keys = _month_keys(now.year, now.month, 12)
        slots = [datetime(year, month, 1, tzinfo=timezone.utc) for year, month in month_keys]
        return "monthly", slots, [slot.strftime("%b %Y") for slot in slots]
    if timeframe_value == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slots = [start + timedelta(hours=hour) for hour in range(24)]
        return "hourly", slots, [slot.strftime("%H:%M") for slot in slots]
    if timeframe_value == "3d":
        start = (now - timedelta(hours=71)).replace(minute=0, second=0, microsecond=0)
        slots = [start + timedelta(hours=idx * 3) for idx in range(24)]
        return "3h", slots, [slot.strftime("%d %b %H:%M") for slot in slots]

    days = _timeframe_span_days(timeframe_value)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    slots = [start + timedelta(days=offset) for offset in range(days)]
    return "daily", slots, [slot.strftime("%d %b") for slot in slots]


def _bucket_index_for_datetime(value: Optional[datetime], mode: str, slots: list[datetime]) -> Optional[int]:
    if value is None or not slots:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)

    if mode == "monthly":
        key = (value.year, value.month)
        for idx, slot in enumerate(slots):
            if (slot.year, slot.month) == key:
                return idx
        return None

    start = slots[0]
    if value < start:
        return None
    if mode == "hourly":
        idx = int((value - start).total_seconds() // 3600)
    elif mode == "3h":
        idx = int((value - start).total_seconds() // (3 * 3600))
    else:
        idx = (value.date() - start.date()).days
    return idx if 0 <= idx < len(slots) else None


def _average_or_none(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _build_duration_chart_data(
    db: Session,
    db_service: DatabaseService,
    *,
    timeframe_value: str,
    category_filter: Optional[str],
) -> dict:
    now = datetime.now(timezone.utc)
    mode, slots, display_labels = _duration_chart_buckets(now, timeframe_value)
    start_date = slots[0] if slots else now

    pipeline_buckets: list[list[float]] = [[] for _ in slots]
    job_buckets: list[list[float]] = [[] for _ in slots]

    pipelines = (
        db.query(ActivePipeline)
        .filter(
            ActivePipeline.finished_at >= start_date,
            ActivePipeline.duration_seconds.isnot(None),
        )
        .order_by(ActivePipeline.finished_at.asc())
        .all()
    )
    for pipeline in pipelines:
        idx = _bucket_index_for_datetime(pipeline.finished_at, mode, slots)
        if idx is not None and pipeline.duration_seconds is not None:
            pipeline_buckets[idx].append(float(pipeline.duration_seconds) / 60)

    jobs_query = (
        db.query(JobReport)
        .filter(
            JobReport.finished_at >= start_date,
            JobReport.duration_seconds.isnot(None),
        )
        .order_by(JobReport.finished_at.asc())
    )
    jobs_query = db_service._apply_job_category_filter(jobs_query, category_filter)
    for job in jobs_query.all():
        idx = _bucket_index_for_datetime(job.finished_at, mode, slots)
        if idx is not None and job.duration_seconds is not None:
            job_buckets[idx].append(float(job.duration_seconds) / 60)

    pipeline_avg = [_average_or_none(values) for values in pipeline_buckets]
    job_avg = [_average_or_none(values) for values in job_buckets]

    return {
        "mode": mode,
        "labels": [slot.isoformat() for slot in slots],
        "display_labels": display_labels,
        "pipeline_avg": pipeline_avg,
        "job_avg": job_avg,
        "pipeline_count": [len(values) for values in pipeline_buckets],
        "job_count": [len(values) for values in job_buckets],
    }


def _normalize_product_group_path(path: Optional[str], name: Optional[str]) -> str:
    raw = (path or "").strip()
    if not raw:
        return (name or "unknown").strip().strip("/") or "unknown"
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        raw = (parsed.path or "").strip()
    raw = raw.strip("/")
    if not raw:
        return (name or "unknown").strip().strip("/") or "unknown"
    segments = [segment for segment in raw.split("/") if segment]
    if not segments:
        return (name or "unknown").strip().strip("/") or "unknown"
    if is_nested_product_namespace(segments[0]) and len(segments) >= 2:
        return f"{segments[0]}/{segments[1]}"
    return segments[0]


def _derive_product_name_from_audit_repo(path_with_namespace: Optional[str]) -> str:
    raw = (path_with_namespace or "").strip().lower()
    segments = [segment for segment in raw.split("/") if segment]
    if not segments:
        return "unknown"
    if is_nested_product_namespace(segments[0]) and len(segments) >= 2:
        return segments[1]
    return segments[0]


def _extract_host(url_or_host: Optional[str]) -> str:
    value = (url_or_host or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    return parsed.netloc or value


def _build_repository_target_index(db: Session) -> dict[tuple[str, str], Repository]:
    rows = (
        db.query(Repository, Product)
        .join(Product, Product.id == Repository.product_id)
        .all()
    )
    index: dict[tuple[str, str], Repository] = {}
    for repository, product in rows:
        host = _extract_host(product.gitlab_base_url).lower().strip("/")
        path = (repository.path or "").strip("/").lower()
        if host and path:
            index[(host, path)] = repository
    return index


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
