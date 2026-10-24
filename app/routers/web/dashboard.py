from fastapi import APIRouter, Depends, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload
from typing import Optional, Iterable, Set, List, Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
import asyncio
import json
import logging
import os
from ...models import (
    SessionLocal,
    Product,
    Repository,
    JobReport,
    ActivePipeline,
    Delivery,
    PipelineStatus,
    PipelineJob,
    AuditCoverageStatus,
    AuditScanRun,
    ConfigScanRun,
    ScannerInstance,
)
from ...database import DatabaseService
from ...audit_service import AuditService
from ...config_scan_service import ConfigScanService, project_counts_for_run
from ...config_scan_reporting import findings_severity_counts, format_scan_duration, gitlab_path_from_product_field
from ...iac_service import IacService
from ...models.iac import IacScanRun
from ...models.job_report import JobStatus
from ...target_navigation import compatibility_redirect_url
from ...pipeline_query_service import PipelineQueryService
import math

from fastapi import APIRouter
from .common import (
    _build_duration_chart_data,
    _format_duration_summary,
    _month_keys,
    _normalize_timeframe,
    _timeframe_span_days,
    get_db,
    templates,
    DASHBOARD_TIMEFRAME_OPTIONS,
    DASHBOARD_TOOL_OPTIONS,
    DASHBOARD_TIMEFRAME_VALUES,
    LOG_TIMEFRAME_OPTIONS,
    LOG_TIMEFRAME_VALUES,
    DASHBOARD_TOOL_VALUES,
    JOB_DURATION_CATEGORY_OPTIONS,
    _format_duration,
    _DISPLAY_TIMEZONE,
    _DISPLAY_TIMEZONE_NAME,
)

router = APIRouter(tags=["web"])
logger = logging.getLogger(__name__)
_SIGNAL_CONTROL_INDEX = Path("app/static/next/index.html")


def _dashboard_signal(
    *,
    topology: dict[str, Any],
    stats: dict[str, Any],
    config_scan: dict[str, int],
    iac: dict[str, Any],
    audit: dict[str, int],
    timeframe: str,
) -> dict[str, str]:
    """Prioritized, evidence-backed explanation for the overview headline."""
    failed_jobs = int(stats.get("failed_jobs_count") or 0)
    config_findings = int(config_scan.get("open_findings") or 0)
    iac_findings = int(iac.get("critical") or 0) + int(iac.get("high") or 0)
    coverage_gaps = int(audit.get("secpipe_missing") or 0) + int(
        audit.get("secpipe_present_not_working") or 0
    )
    issue_parts = [
        f"{failed_jobs} failed jobs" if failed_jobs else "",
        f"{config_findings} configuration findings" if config_findings else "",
        f"{iac_findings} critical/high IaC findings" if iac_findings else "",
        f"{coverage_gaps} coverage gaps" if coverage_gaps else "",
    ]
    issue_context = " · ".join(part for part in issue_parts if part)

    if failed_jobs:
        action_label = "Inspect failed jobs"
        action_href = f"/logs?status=failed&timeframe={timeframe}"
    else:
        action_label = "Open executions"
        action_href = f"/logs?timeframe={timeframe}"

    telemetry_state = topology.get("telemetry_state") or "empty"
    if telemetry_state == "stale":
        return {
            "tone": "stale",
            "title": f"Latest execution signal arrived {topology.get('last_execution_label') or 'earlier'}.",
            "context": "Showing last known state"
            + (f" · {issue_context}" if issue_context else ""),
            "action_label": action_label,
            "action_href": action_href,
        }
    if telemetry_state == "empty":
        return {
            "tone": "empty",
            "title": "No execution telemetry has been recorded yet.",
            "context": "Connect a source or run a pipeline to establish the first operational signal.",
            "action_label": "Open integrations",
            "action_href": "/integrations",
        }
    if failed_jobs:
        return {
            "tone": "problem",
            "title": f"{failed_jobs} failed jobs need operator context.",
            "context": issue_context,
            "action_label": action_label,
            "action_href": action_href,
        }
    if config_findings or iac_findings or coverage_gaps:
        return {
            "tone": "warning",
            "title": "Execution delivery is current, with posture findings to review.",
            "context": issue_context,
            "action_label": "Review coverage",
            "action_href": "/coverage",
        }
    return {
        "tone": "healthy",
        "title": "Execution telemetry is current.",
        "context": "No critical execution or posture backlog is visible in this window.",
        "action_label": "Open executions",
        "action_href": action_href,
    }


def _config_scan_compact(db: Session) -> dict[str, int]:
    """Latest successful config-scan totals, one current snapshot per product."""
    latest_cfg_run = (
        db.query(ConfigScanRun.product_id, func.max(ConfigScanRun.id).label("run_id"))
        .filter(ConfigScanRun.status == "succeeded")
        .group_by(ConfigScanRun.product_id)
        .subquery()
    )
    rows = (
        db.query(ConfigScanRun.scanned_projects, ConfigScanRun.findings_count)
        .join(latest_cfg_run, ConfigScanRun.id == latest_cfg_run.c.run_id)
        .all()
    )
    return {
        "products_scanned": sum(1 for scanned, _ in rows if (scanned or 0) > 0),
        "open_findings": sum(int(findings or 0) for _, findings in rows),
    }


def _overview_failure_hotspots(
    db: Session,
    timeframe: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return grouped unresolved failures for the selected operator window.

    The queue uses the same active-failure definition as Job journal and the
    Overview Attention count. Grouping by repository and job makes this a
    useful drill-down without exposing another competing failure denominator.
    """
    service = DatabaseService(db)
    query = service._active_failed_jobs_query(
        service._resolve_timeframe_cutoff(timeframe)
    ).join(Product, Product.id == Repository.product_id)
    rows = (
        query.with_entities(
            JobReport.repository_id,
            Repository.path,
            Product.name,
            JobReport.job_name,
            func.count(JobReport.id).label("open_failures"),
            func.max(JobReport.finished_at).label("last_failure_at"),
        )
        .group_by(
            JobReport.repository_id,
            Repository.path,
            Product.name,
            JobReport.job_name,
        )
        .order_by(
            func.count(JobReport.id).desc(),
            func.max(JobReport.finished_at).desc(),
        )
        .limit(limit)
        .all()
    )
    return [
        {
            "repository_id": repository_id,
            "repository": repository_path or "Unknown repository",
            "product": product_name,
            "job": job_name,
            "open_failures": int(open_failures or 0),
            "last_failure_at": (
                last_failure_at.isoformat() if last_failure_at else None
            ),
            "href": (
                f"/logs?status=failed&timeframe={quote(timeframe)}"
                f"&repo={quote(repository_path or '', safe='')}"
                f"&job={quote(job_name or '', safe='')}"
            ),
        }
        for (
            repository_id,
            repository_path,
            product_name,
            job_name,
            open_failures,
            last_failure_at,
        ) in rows
    ]


def _overview_platform_summary(db: Session) -> dict[str, Any]:
    """Compact persisted scanner/connector health for conditional Overview use."""
    instances = db.query(ScannerInstance).order_by(ScannerInstance.name.asc()).all()
    healthy_states = {"healthy", "success"}
    problem_rows = [
        {
            "name": instance.name,
            "status": str(instance.health_status or "unknown").lower(),
            "reason": instance.health_reason,
            "last_seen_at": (
                instance.last_seen_at.isoformat()
                if instance.last_seen_at
                else None
            ),
        }
        for instance in instances
        if str(instance.health_status or "unknown").lower() not in healthy_states
    ]
    return {
        "instances_total": len(instances),
        "instances_healthy": len(instances) - len(problem_rows),
        "instances_problem": len(problem_rows),
        "problems": problem_rows[:4],
    }


def _overview_control_summary(db: Session) -> dict[str, Any]:
    """Aggregate current control bindings for the Overview posture snapshot."""
    from ...control_coverage import coverage_by_repository

    repository_ids = [row[0] for row in db.query(Repository.id).all()]
    controls = coverage_by_repository(db, repository_ids)
    expected = sum(int(item.get("expected", 0)) for item in controls.values())
    configured = sum(int(item.get("configured", 0)) for item in controls.values())
    return {
        "expected": expected,
        "configured": configured,
        "missing": max(expected - configured, 0),
        "rate": round(configured / expected * 100, 1) if expected else None,
    }


_OVERVIEW_DURATION_CATEGORY_LABELS = {
    "sast": "SAST",
    "sca": "SCA",
    "secrets": "Secrets",
    "cs": "Container security",
    "asoc": "ASOC",
    "mobsf": "MobSF",
    "other": "Other checks",
}
_OVERVIEW_DURATION_CATEGORY_ORDER = tuple(_OVERVIEW_DURATION_CATEGORY_LABELS)


def _overview_duration_category(job_name: Optional[str]) -> str:
    """Use the same check-family semantics as Job journal's duration filter."""
    normalized = (job_name or "").strip().lower()
    if normalized in {"ptai_scan", "ptai_async_scan", "semgrep_scan"}:
        return "sast"
    if normalized.startswith("sca"):
        return "sca"
    if normalized == "gitleaks_scan":
        return "secrets"
    if normalized == "trivy_scan":
        return "cs"
    if normalized.startswith("asoc"):
        return "asoc"
    if normalized == "mobsf_scan":
        return "mobsf"
    return "other"


def _overview_duration_breakdown(
    db: Session,
    timeframe: str,
    *,
    job_limit: int = 8,
    category_filter: Optional[str] = None,
) -> dict[str, Any]:
    """Return pipeline runtime plus comparable job/check-family durations.

    Job runtimes are reported as distributions rather than summed: scanners
    commonly run in parallel, so a sum would imply a false end-to-end time.
    """
    service = DatabaseService(db)
    pipeline = _format_duration_summary(
        service.get_pipeline_duration_summary(timeframe=timeframe)
    )
    cutoff = service._resolve_timeframe_cutoff(timeframe)
    query = db.query(JobReport.job_name, JobReport.duration_seconds).join(
        Repository, JobReport.repository_id == Repository.id
    )
    query = query.filter(JobReport.duration_seconds.isnot(None))
    if cutoff:
        query = query.filter(JobReport.finished_at >= cutoff)
    query = service._apply_job_category_filter(query, category_filter)

    category_durations: dict[str, list[float]] = {
        key: [] for key in _OVERVIEW_DURATION_CATEGORY_ORDER
    }
    job_durations: dict[str, list[float]] = {}
    for job_name, duration in query.all():
        if duration is None or duration < 0:
            continue
        value = float(duration)
        name = (job_name or "Other check").strip() or "Other check"
        category_durations[_overview_duration_category(name)].append(value)
        job_durations.setdefault(name, []).append(value)

    def present(key: str, values: list[float]) -> dict[str, Any]:
        summary = _format_duration_summary(
            DatabaseService._summarize_durations(values)
        )
        return {
            "key": key,
            "label": _OVERVIEW_DURATION_CATEGORY_LABELS.get(key, key),
            "count": int(summary["count"]),
            "median": summary.get("median"),
            "median_label": summary.get("median_label") or "—",
            "average": summary.get("avg"),
            "average_label": summary.get("avg_label") or "—",
        }

    categories = [
        present(key, values)
        for key, values in category_durations.items()
        if values
    ]
    jobs = [
        present(name, values)
        for name, values in sorted(
            job_durations.items(),
            key=lambda item: (-len(item[1]), -sum(item[1]) / len(item[1]), item[0]),
        )[:job_limit]
    ]
    return {
        "pipeline": {
            "count": int(pipeline.get("count") or 0),
            "median": pipeline.get("median"),
            "median_label": pipeline.get("median_label") or "—",
            "average": pipeline.get("avg"),
            "average_label": pipeline.get("avg_label") or "—",
        },
        "categories": categories,
        "jobs": jobs,
    }


def _topology_metrics(
    db: Session,
    timeframe: str,
    *,
    stats: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Live values behind the five Signal River stages.

    A normalized output is a persisted ``JobReport``: raw GitLab job payloads
    are converted to that common record by the ingest/poller path before the UI
    can observe them.
    """
    db_service = DatabaseService(db)
    stats = stats or db_service.get_dashboard_stats(timeframe=timeframe)
    cutoff = db_service._resolve_timeframe_cutoff(timeframe)
    pipeline_health = PipelineQueryService(db).execution_health(timeframe)
    timeframe_label = next(
        (
            option["label"]
            for option in LOG_TIMEFRAME_OPTIONS
            if option["value"] == pipeline_health.timeframe
        ),
        pipeline_health.timeframe,
    )
    job_outcome_query = db.query(
        JobReport.job_status,
        func.count(JobReport.id),
    ).filter(JobReport.job_status.in_([JobStatus.SUCCESS, JobStatus.FAILED]))
    if cutoff:
        job_outcome_query = job_outcome_query.filter(
            JobReport.finished_at >= cutoff
        )
    job_outcomes = {
        status: int(count)
        for status, count in job_outcome_query.group_by(
            JobReport.job_status
        ).all()
    }
    successful_jobs = job_outcomes.get(JobStatus.SUCCESS, 0)
    failed_jobs = job_outcomes.get(JobStatus.FAILED, 0)
    terminal_jobs = successful_jobs + failed_jobs
    job_success_rate = (
        round((successful_jobs / terminal_jobs) * 100, 1)
        if terminal_jobs
        else 0
    )
    job_failure_rate = (
        round((failed_jobs / terminal_jobs) * 100, 1)
        if terminal_jobs
        else 0
    )

    trigger_query = db.query(ActivePipeline)
    if cutoff:
        trigger_query = trigger_query.filter(ActivePipeline.created_at >= cutoff)

    active_pipelines = (
        db.query(ActivePipeline)
        .filter(ActivePipeline.status.in_([PipelineStatus.REGISTERED, PipelineStatus.POLLING]))
        .count()
    )
    mapped_products = (
        db.query(Product)
        .filter(
            Product.asoc_product_id.isnot(None),
            Product.asoc_product_id != "",
        )
        .count()
    )
    confirmed_delivery_query = db.query(Delivery).filter(
        Delivery.status == "confirmed",
        or_(
            Delivery.destination == "asoc",
            Delivery.destination.like("asoc:%"),
        ),
    )
    if cutoff:
        confirmed_delivery_query = confirmed_delivery_query.filter(
            func.coalesce(
                Delivery.confirmed_at,
                Delivery.attempted_at,
                Delivery.created_at,
            )
            >= cutoff
        )
    asoc_confirmed_deliveries = confirmed_delivery_query.count()
    config_scan = _config_scan_compact(db)
    last_execution_at = db.query(func.max(JobReport.finished_at)).scalar()
    if last_execution_at is not None and last_execution_at.tzinfo is None:
        last_execution_at = last_execution_at.replace(tzinfo=timezone.utc)
    telemetry_age_seconds = (
        max(0, int((datetime.now(timezone.utc) - last_execution_at).total_seconds()))
        if last_execution_at
        else None
    )
    telemetry_stale_after = max(60, int(os.getenv("TOPOLOGY_SIGNAL_STALE_SECONDS", "86400")))
    telemetry_state = (
        "empty"
        if telemetry_age_seconds is None
        else "stale"
        if telemetry_age_seconds > telemetry_stale_after
        else "live"
    )
    if telemetry_age_seconds is None:
        last_execution_label = "not recorded"
    elif telemetry_age_seconds < 60:
        last_execution_label = "just now"
    elif telemetry_age_seconds < 3600:
        last_execution_label = f"{max(1, round(telemetry_age_seconds / 60))}m ago"
    elif telemetry_age_seconds < 86400:
        last_execution_label = f"{max(1, round(telemetry_age_seconds / 3600))}h ago"
    else:
        last_execution_label = f"{max(1, round(telemetry_age_seconds / 86400))}d ago"
    return {
        "observed_steps": stats["total_jobs"],
        "target_count": stats["total_repositories"],
        "gitlab_triggers": trigger_query.count(),
        "active_pipelines": active_pipelines,
        # The headline outcome is deliberately job-based: Job journal rows are
        # the operator's auditable execution unit. Pipeline outcomes remain
        # available below under explicit pipeline_* names.
        "success_rate": job_success_rate,
        "failure_rate": job_failure_rate,
        "execution_completed": successful_jobs,
        "execution_failed": failed_jobs,
        "execution_total": terminal_jobs,
        "execution_timeframe": pipeline_health.timeframe,
        "execution_timeframe_label": timeframe_label,
        "pipeline_success_rate": pipeline_health.success_rate,
        "pipeline_failure_rate": pipeline_health.failure_rate,
        "pipeline_completed": pipeline_health.completed,
        "pipeline_failed": pipeline_health.failed,
        "pipeline_total": pipeline_health.total,
        "normalized_outputs": stats["total_jobs"],
        "failed_outputs": stats["failed_jobs_count"],
        "config_drift": config_scan["open_findings"],
        "config_products_scanned": config_scan["products_scanned"],
        "asoc_confirmed_deliveries": asoc_confirmed_deliveries,
        "asoc_mapped_products": mapped_products,
        "total_products": stats["total_products"],
        # The API can be reachable while the observed security flow is old.
        # Expose the source timestamp separately so the dashboard never
        # presents a fresh HTTP response as fresh execution telemetry.
        "last_execution_at": last_execution_at.isoformat() if last_execution_at else None,
        "last_execution_label": last_execution_label,
        "telemetry_age_seconds": telemetry_age_seconds,
        "telemetry_stale_after_seconds": telemetry_stale_after,
        "telemetry_state": telemetry_state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _status_counts_by_bucket(
    db,
    db_service,
    start_date,
    trunc,
    category_filter,
    timezone_name: str = "UTC",
    end_at: datetime | None = None,
):
    """Success/failed job counts per time bucket, aggregated in SQL.

    Replaces the previous "load every job row in the window and bucket them in
    Python" approach, which scaled linearly with job volume (≈1s for a year of
    data). A ``GROUP BY date_trunc`` returns at most a few hundred bucket rows
    regardless of how many jobs are in the window. ``timezone('UTC', …)`` forces
    UTC bucketing independent of the DB session timezone, matching the old
    ``astimezone(utc)`` logic. Keys are naive-UTC datetimes.
    """
    bucket = func.date_trunc(trunc, func.timezone(timezone_name, JobReport.finished_at))
    query = db.query(bucket.label("bucket"), JobReport.job_status, func.count(JobReport.id)).filter(
        JobReport.finished_at >= start_date,
        JobReport.job_status.in_([JobStatus.SUCCESS, JobStatus.FAILED]),
    )
    if end_at is not None:
        query = query.filter(JobReport.finished_at <= end_at)
    query = db_service._apply_job_category_filter(query, category_filter)
    counts: dict = {}
    for bucket_dt, status, count in query.group_by("bucket", JobReport.job_status).all():
        if bucket_dt is None:
            continue
        slot = counts.setdefault(bucket_dt, {JobStatus.SUCCESS: 0, JobStatus.FAILED: 0})
        slot[status] = slot.get(status, 0) + count
    return counts


def _build_signal_throughput(db, db_service, timeframe_value: str) -> dict[str, Any]:
    """Build the Overview chart using the same period semantics as the legacy view.

    The returned starts/ends are timezone-aware so the client can keep the
    tooltip and axis tied to the exact bucket represented by each bar.
    """
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    now_local = now_utc.astimezone(_DISPLAY_TIMEZONE)
    timezone_name = _DISPLAY_TIMEZONE_NAME
    query_start_utc: datetime | None = None

    if timeframe_value == "today":
        granularity = "hour"
        query_start_utc = now_utc.replace(hour=0, minute=0, second=0)
        start_local = query_start_utc.astimezone(_DISPLAY_TIMEZONE)
        bucket_count = 24
        step = timedelta(hours=1)
        label_format = "%H:%M"
        window_label = "Today · hourly · UTC day"
    elif timeframe_value == "3d":
        granularity = "3h"
        query_start_utc = now_utc - timedelta(hours=72)
        start_local = query_start_utc.astimezone(_DISPLAY_TIMEZONE).replace(
            minute=0, second=0, microsecond=0
        )
        bucket_count = 25
        step = timedelta(hours=3)
        label_format = "%d %b %H:%M"
        window_label = "Last 3 days · 3-hour buckets"
    elif timeframe_value in {"7d", "30d"}:
        granularity = "day"
        span_days = 7 if timeframe_value == "7d" else 30
        query_start_utc = now_utc - timedelta(days=span_days)
        start_local = query_start_utc.astimezone(_DISPLAY_TIMEZONE).replace(
            hour=0, minute=0, second=0
        )
        bucket_count = span_days + 1
        step = timedelta(days=1)
        label_format = "%d %b"
        window_label = f"Last {span_days} days · daily buckets"
    elif timeframe_value == "1y":
        granularity = "month"
        current_month_index = now_local.year * 12 + now_local.month - 1
        cutoff_utc = now_utc - timedelta(days=365)
        query_start_utc = cutoff_utc
        cutoff_local = cutoff_utc.astimezone(_DISPLAY_TIMEZONE)
        start_month_index = cutoff_local.year * 12 + cutoff_local.month - 1
        start_local = now_local.replace(
            year=start_month_index // 12,
            month=start_month_index % 12 + 1,
            day=1,
            hour=0,
            minute=0,
            second=0,
        )
        bucket_count = current_month_index - start_month_index + 1
        step = None
        label_format = "%b %Y"
        window_label = "Last 12 months · monthly buckets"
    else:
        earliest = db.query(func.min(JobReport.finished_at)).filter(
            JobReport.finished_at.isnot(None),
            JobReport.job_status.in_([JobStatus.SUCCESS, JobStatus.FAILED]),
        ).scalar()
        if earliest is None:
            return {
                "buckets": [],
                "window_label": "All retained history",
                "timezone": timezone_name,
                "granularity": "empty",
            }
        earliest_utc = earliest if earliest.tzinfo else earliest.replace(tzinfo=timezone.utc)
        span_days = (now_local.date() - earliest_utc.astimezone(_DISPLAY_TIMEZONE).date()).days + 1
        if span_days <= 31:
            granularity = "day"
            start_local = earliest_utc.astimezone(_DISPLAY_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
            bucket_count = span_days
            step = timedelta(days=1)
            label_format = "%d %b"
            window_label = "All retained history · daily buckets"
        elif span_days <= 90:
            granularity = "3d"
            start_local = earliest_utc.astimezone(_DISPLAY_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
            bucket_count = math.ceil(span_days / 3)
            step = timedelta(days=3)
            label_format = "%d %b"
            window_label = "All retained history · 3-day buckets"
        else:
            granularity = "month"
            first = earliest_utc.astimezone(_DISPLAY_TIMEZONE)
            start_local = first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            bucket_count = (now_local.year - start_local.year) * 12 + now_local.month - start_local.month + 1
            step = None
            label_format = "%b %Y"
            window_label = "All retained history · monthly buckets"

    start_utc = query_start_utc or start_local.astimezone(timezone.utc)
    trunc = (
        "month"
        if granularity == "month"
        else "day"
        if granularity in {"day", "3d"}
        else "hour"
    )
    counts = _status_counts_by_bucket(
        db,
        db_service,
        start_utc,
        trunc,
        None,
        timezone_name=timezone_name,
        end_at=now_utc,
    )

    def month_start(value: datetime) -> datetime:
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def add_months(value: datetime, amount: int) -> datetime:
        index = value.year * 12 + value.month - 1 + amount
        return value.replace(year=index // 12, month=index % 12 + 1, day=1)

    buckets: list[dict[str, Any]] = []
    for offset in range(bucket_count):
        if granularity == "month":
            bucket_start = add_months(month_start(start_local), offset)
            bucket_end = add_months(bucket_start, 1)
            bucket = counts.get(bucket_start.replace(tzinfo=None), {})
        else:
            bucket_start = start_local + step * offset
            bucket_end = bucket_start + step
            if granularity in {"3h", "3d"}:
                bucket = {}
                increment = timedelta(hours=1) if granularity == "3h" else timedelta(days=1)
                for interval_offset in range(3):
                    interval_bucket = counts.get(
                        (bucket_start + increment * interval_offset).replace(tzinfo=None),
                        {},
                    )
                    bucket[JobStatus.SUCCESS] = bucket.get(JobStatus.SUCCESS, 0) + interval_bucket.get(JobStatus.SUCCESS, 0)
                    bucket[JobStatus.FAILED] = bucket.get(JobStatus.FAILED, 0) + interval_bucket.get(JobStatus.FAILED, 0)
            else:
                bucket = counts.get(bucket_start.replace(tzinfo=None), {})
        reported_end = (
            bucket_start
            if bucket_start >= now_local
            else min(bucket_end, now_local)
        )
        observed_start = bucket_start
        if query_start_utc is not None:
            observed_start = max(observed_start, query_start_utc.astimezone(_DISPLAY_TIMEZONE))
        partial = observed_start != bucket_start or reported_end != bucket_end
        range_format = "%d %b %H:%M" if partial else label_format
        buckets.append(
            {
                "label": bucket_start.strftime(label_format),
                "range_label": f"{observed_start.strftime(range_format)}–{reported_end.strftime(range_format)} {timezone_name}",
                "start_at": bucket_start.isoformat(),
                "end_at": reported_end.isoformat(),
                "is_current": bucket_start <= now_local < bucket_end,
                "succeeded": int(bucket.get(JobStatus.SUCCESS, 0)),
                "failed": int(bucket.get(JobStatus.FAILED, 0)),
            }
        )
    return {
        "buckets": buckets,
        "window_label": window_label,
        "timezone": timezone_name,
        "granularity": granularity,
    }


@router.get("/classic", response_class=RedirectResponse, include_in_schema=False)
async def dashboard_compatibility(request: Request):
    """Keep bookmarked legacy entrypoints without exposing a second UI."""
    return RedirectResponse(
        compatibility_redirect_url(request, "/"),
        status_code=308,
    )


@router.get(
    "/overview/operator",
    response_class=RedirectResponse,
    include_in_schema=False,
)
async def operator_overview_compatibility(request: Request):
    """Keep old operator bookmarks on the single canonical Overview."""
    return RedirectResponse(
        compatibility_redirect_url(request, "/"),
        status_code=308,
    )


async def _legacy_dashboard_render(
    request: Request,
    timeframe: str = "7d",
    tool: str = "all",
    db: Session = Depends(get_db),
):
    """Main dashboard page"""
    db_service = DatabaseService(db)
    timeframe_value = _normalize_timeframe(timeframe, DASHBOARD_TIMEFRAME_VALUES, "7d")
    tool_value = _normalize_timeframe(tool, DASHBOARD_TOOL_VALUES, "all")
    stats = db_service.get_dashboard_stats(timeframe=timeframe_value)
    topology = _topology_metrics(db, timeframe_value, stats=stats)
    audit_service = AuditService(db)
    audit_summary = audit_service.get_latest_summary()
    audit_compact = {
        "total": audit_summary["covered"] + audit_summary["pipeline_no_sec"] + audit_summary["no_pipelines"] + audit_summary["inaccessible"],
        "secpipe_working": audit_summary["covered"],
        "secpipe_missing": audit_summary["pipeline_no_sec"],
        "secpipe_present_not_working": audit_summary["no_pipelines"] + audit_summary["inaccessible"],
    }

    # Dashboard config compact: aggregate the latest succeeded run per product in
    # ONE query instead of building (and JSON-parsing) a full overview bundle for
    # every product (N+1: 1 query + report parse per product). Verified to match
    # the per-product bundle totals (products_scanned / open_findings).
    config_scan_compact = {
        "products_scanned": topology["config_products_scanned"],
        "open_findings": topology["config_drift"],
    }
    iac_compact = IacService(db).dashboard()

    selected_category = None if tool_value == "all" else tool_value
    pipeline_duration = _format_duration_summary(
        db_service.get_pipeline_duration_summary(timeframe=timeframe_value)
    )
    pipeline_duration_tail = (
        max(0, pipeline_duration["p95"] - pipeline_duration["median"])
        if pipeline_duration.get("p95") is not None
        and pipeline_duration.get("median") is not None
        else None
    )
    pipeline_duration["p95_tail_label"] = (
        _format_duration(pipeline_duration_tail)
        if pipeline_duration_tail is not None
        else "—"
    )
    job_duration = _format_duration_summary(
        db_service.get_job_duration_summary(timeframe=timeframe_value, category_filter=selected_category)
    )
    job_duration_breakdown = []
    for option in JOB_DURATION_CATEGORY_OPTIONS:
        summary = _format_duration_summary(
            db_service.get_job_duration_summary(
                timeframe=timeframe_value,
                category_filter=option["value"],
            )
        )
        job_duration_breakdown.append({**option, "summary": summary})

    now = datetime.now(timezone.utc)
    chart_data = {"labels": [], "display_labels": [], "failed": [], "success": []}

    category = None if tool_value == "all" else tool_value

    if timeframe_value == "1y":
        chart_mode = "monthly"
        month_keys = _month_keys(now.year, now.month, 12)
        start_year, start_month = month_keys[0]
        start_date = datetime(start_year, start_month, 1, tzinfo=timezone.utc)
        counts = _status_counts_by_bucket(db, db_service, start_date, "month", category)
        for year, month in month_keys:
            slot = counts.get(datetime(year, month, 1), {})
            chart_data["labels"].append(f"{year:04d}-{month:02d}-01")
            chart_data["display_labels"].append(datetime(year, month, 1).strftime("%b %Y"))
            chart_data["failed"].append(slot.get(JobStatus.FAILED, 0))
            chart_data["success"].append(slot.get(JobStatus.SUCCESS, 0))
    elif timeframe_value == "today":
        chart_mode = "hourly"
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        counts = _status_counts_by_bucket(db, db_service, start_date, "hour", category)
        for hour in range(24):
            slot = start_date + timedelta(hours=hour)
            bucket = counts.get(slot.replace(tzinfo=None), {})
            chart_data["labels"].append(slot.isoformat())
            chart_data["display_labels"].append(slot.strftime("%H:%M"))
            chart_data["failed"].append(bucket.get(JobStatus.FAILED, 0))
            chart_data["success"].append(bucket.get(JobStatus.SUCCESS, 0))
    elif timeframe_value == "3d":
        chart_mode = "3h"
        start_date = (now - timedelta(hours=71)).replace(minute=0, second=0, microsecond=0)
        bucket_size_hours = 3
        bucket_count = 24
        counts = _status_counts_by_bucket(db, db_service, start_date, "hour", category)
        for idx in range(bucket_count):
            slot = start_date + timedelta(hours=idx * bucket_size_hours)
            failed_count = success_count = 0
            for offset in range(bucket_size_hours):
                bucket = counts.get((slot + timedelta(hours=offset)).replace(tzinfo=None), {})
                failed_count += bucket.get(JobStatus.FAILED, 0)
                success_count += bucket.get(JobStatus.SUCCESS, 0)
            chart_data["labels"].append(slot.isoformat())
            chart_data["display_labels"].append(slot.strftime("%d %b %H:%M"))
            chart_data["failed"].append(failed_count)
            chart_data["success"].append(success_count)
    else:
        chart_mode = "daily"
        bucket_days = _timeframe_span_days(timeframe_value)
        start_date = (now - timedelta(days=bucket_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        counts = _status_counts_by_bucket(db, db_service, start_date, "day", category)
        for offset in range(bucket_days):
            day = (start_date + timedelta(days=offset)).date()
            bucket = counts.get(datetime(day.year, day.month, day.day), {})
            chart_data["labels"].append(day.isoformat())
            chart_data["display_labels"].append(day.strftime("%d %b"))
            chart_data["failed"].append(bucket.get(JobStatus.FAILED, 0))
            chart_data["success"].append(bucket.get(JobStatus.SUCCESS, 0))

    chart_data["mode"] = chart_mode
    successful_executions = sum(chart_data["success"])
    failed_executions = sum(chart_data["failed"])
    total_executions = successful_executions + failed_executions
    chart_summary = {
        "successful": successful_executions,
        "failed": failed_executions,
        "total": total_executions,
        "failure_rate": round((failed_executions / total_executions) * 100, 1) if total_executions else 0,
    }
    overview_signal = _dashboard_signal(
        topology=topology,
        stats=stats,
        config_scan=config_scan_compact,
        iac=iac_compact,
        audit=audit_compact,
        timeframe=timeframe_value,
    )
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "active_pipeline_count": topology["active_pipelines"],
        "topology": topology,
        "audit_summary": audit_summary,
        "audit_compact": audit_compact,
        "config_scan_compact": config_scan_compact,
        "iac_compact": iac_compact,
        "chart_data": chart_data,
        "chart_summary": chart_summary,
        "overview_signal": overview_signal,
        "chart_timeframes": DASHBOARD_TIMEFRAME_OPTIONS,
        "chart_tools": DASHBOARD_TOOL_OPTIONS,
        "selected_timeframe": timeframe_value,
        "selected_tool": tool_value,
        "pipeline_duration": pipeline_duration,
        "job_duration": job_duration,
        "job_duration_breakdown": job_duration_breakdown,
    })


@router.get("/dashboard/topology", response_class=JSONResponse)
async def dashboard_topology(
    timeframe: str = "7d",
    db: Session = Depends(get_db),
):
    """Small polling payload for the live Signal River, without a page reload."""
    timeframe_value = _normalize_timeframe(timeframe, DASHBOARD_TIMEFRAME_VALUES, "7d")
    return JSONResponse(_topology_metrics(db, timeframe_value))


@router.get("/api/ui/overview", response_class=JSONResponse)
async def signal_control_overview(
    timeframe: str = Query("7d", max_length=16),
    db: Session = Depends(get_db),
):
    """Real-data adapter for the parallel Signal Control overview.

    The React presentation layer deliberately consumes a compact, stable view
    model instead of knowing about database tables or the legacy template
    context.
    """
    timeframe_value = _normalize_timeframe(
        timeframe,
        LOG_TIMEFRAME_VALUES,
        "7d",
    )
    db_service = DatabaseService(db)
    stats = db_service.get_dashboard_stats(timeframe=timeframe_value)
    topology = _topology_metrics(db, timeframe_value, stats=stats)

    audit_summary = AuditService(db).get_latest_summary()
    audit_covered = int(audit_summary["covered"] or 0)
    audit_missing = int(audit_summary["pipeline_no_sec"] or 0)
    audit_no_pipeline = int(audit_summary["no_pipelines"] or 0)
    audit_inaccessible = int(audit_summary["inaccessible"] or 0)
    coverage_gaps = audit_missing + audit_no_pipeline + audit_inaccessible
    audited_targets = audit_covered + audit_missing + audit_no_pipeline + audit_inaccessible
    iac_compact = IacService(db).dashboard()
    iac_critical_high = int(iac_compact["critical"] or 0) + int(
        iac_compact["high"] or 0
    )
    config_scan = {
        "products_scanned": int(topology["config_products_scanned"] or 0),
        "open_findings": int(topology["config_drift"] or 0),
    }
    audit_compact = {
        "total": audited_targets,
        "secpipe_working": audit_covered,
        "secpipe_missing": audit_missing,
        "secpipe_present_not_working": int(
            audit_no_pipeline + audit_inaccessible
        ),
    }
    overview_signal = _dashboard_signal(
        topology=topology,
        stats=stats,
        config_scan=config_scan,
        iac=iac_compact,
        audit=audit_compact,
        timeframe=timeframe_value,
    )
    control_summary = _overview_control_summary(db)
    platform_summary = _overview_platform_summary(db)
    hotspots = _overview_failure_hotspots(db, timeframe_value)

    throughput = _build_signal_throughput(db, db_service, timeframe_value)

    duration = _format_duration_summary(
        db_service.get_pipeline_duration_summary(timeframe=timeframe_value)
    )
    duration_breakdown = _overview_duration_breakdown(db, timeframe_value)
    median_duration = duration.get("median")
    p95_duration = duration.get("p95")
    p95_tail = (
        max(0, p95_duration - median_duration)
        if p95_duration is not None and median_duration is not None
        else None
    )
    return JSONResponse(
        {
            "topology": topology,
            "attention": {
                # Attention links to the unresolved failure queue, so its count
                # must use the same unbounded scope as that destination instead
                # of the 24-hour throughput window above.
                "failed_jobs": db_service.count_active_failed_jobs(),
                "failed_jobs_in_window": int(stats["failed_jobs_count"] or 0),
                "config_drift": int(topology["config_drift"] or 0),
                "config_products_scanned": int(topology["config_products_scanned"] or 0),
                "iac_critical_high": iac_critical_high,
                "iac_repositories_total": int(iac_compact["repositories_total"] or 0),
                "iac_repositories_scanned": int(iac_compact["scanned_repositories"] or 0),
                "coverage_gaps": coverage_gaps,
                "coverage_targets_total": audited_targets,
            },
            "overview_signal": overview_signal,
            "hotspots": hotspots,
            "posture": {
                "audit_covered": audit_covered,
                "audit_targets": audited_targets,
                "audit_gaps": coverage_gaps,
                "audit_rate": (
                    round(audit_covered / audited_targets * 100, 1)
                    if audited_targets
                    else None
                ),
                "controls_expected": control_summary["expected"],
                "controls_configured": control_summary["configured"],
                "controls_missing": control_summary["missing"],
                "control_rate": control_summary["rate"],
                "iac_critical_high": iac_critical_high,
                "iac_repositories_scanned": int(iac_compact["scanned_repositories"] or 0),
                "config_findings": int(topology["config_drift"] or 0),
                "config_products_scanned": int(topology["config_products_scanned"] or 0),
                "asoc_mapped_products": int(topology["asoc_mapped_products"] or 0),
                "total_products": int(topology["total_products"] or 0),
            },
            "platform": platform_summary,
            "throughput": throughput["buckets"],
            "throughput_window_label": throughput["window_label"],
            "throughput_timezone": throughput["timezone"],
            "throughput_granularity": throughput["granularity"],
            "latency": {
                "sample_count": int(duration.get("count") or 0),
                "average": duration.get("avg"),
                "average_label": duration.get("avg_label") or "—",
                "median": duration.get("median"),
                "median_label": duration.get("median_label") or "—",
                "p95": duration.get("p95"),
                "p95_label": duration.get("p95_label") or "—",
                "p95_tail": p95_tail,
                "p95_tail_label": _format_duration(p95_tail),
            },
            "duration_breakdown": duration_breakdown,
        }
    )


@router.get("/dashboard/duration", response_class=FileResponse)
async def dashboard_duration(
):
    """Serve the canonical Signal Control detail shell for duration analysis."""
    return FileResponse(_SIGNAL_CONTROL_INDEX, media_type="text/html")


@router.get("/api/ui/duration", response_class=JSONResponse)
async def signal_control_duration(
    timeframe: str = Query("7d", max_length=16),
    tool: str = Query("all", max_length=16),
    db: Session = Depends(get_db),
):
    """Return the real-data model for the canonical execution-duration view."""
    timeframe_value = _normalize_timeframe(
        timeframe,
        LOG_TIMEFRAME_VALUES,
        "7d",
    )
    tool_value = _normalize_timeframe(tool, DASHBOARD_TOOL_VALUES, "all")
    selected_category = None if tool_value == "all" else tool_value
    db_service = DatabaseService(db)
    pipeline = _format_duration_summary(
        db_service.get_pipeline_duration_summary(timeframe=timeframe_value)
    )
    jobs = _format_duration_summary(
        db_service.get_job_duration_summary(
            timeframe=timeframe_value,
            category_filter=selected_category,
        )
    )
    breakdown = _overview_duration_breakdown(
        db,
        timeframe_value,
        job_limit=24,
        category_filter=selected_category,
    )
    trend = (
        _build_duration_chart_data(
            db,
            db_service,
            timeframe_value=timeframe_value,
            category_filter=selected_category,
        )
        if timeframe_value != "all"
        else None
    )
    return JSONResponse(
        {
            "timeframe": timeframe_value,
            "pipeline": {
                "count": int(pipeline.get("count") or 0),
                "average": pipeline.get("avg"),
                "average_label": pipeline.get("avg_label") or "—",
                "median": pipeline.get("median"),
                "median_label": pipeline.get("median_label") or "—",
                "p95": pipeline.get("p95"),
                "p95_label": pipeline.get("p95_label") or "—",
            },
            "jobs": {
                "count": int(jobs.get("count") or 0),
                "average": jobs.get("avg"),
                "average_label": jobs.get("avg_label") or "—",
                "median": jobs.get("median"),
                "median_label": jobs.get("median_label") or "—",
                "p95": jobs.get("p95"),
                "p95_label": jobs.get("p95_label") or "—",
            },
            "categories": breakdown["categories"],
            "job_breakdown": breakdown["jobs"],
            "trend": trend,
        }
    )
