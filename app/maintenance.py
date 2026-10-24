"""One-off data backfills, executed exactly once via the migrations runner.

Historically ``_backfill_duration_columns`` lived in ``app/main.py`` and ran a
full table scan of job_reports JOIN job_report_details on EVERY process start.
It is registered as a tracked python migration now: runs once, recorded in
``schema_migrations``, never scans again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


def _parse_duration_seconds(value):
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_between_seconds(started_at, finished_at):
    if not started_at or not finished_at:
        return None
    start = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    finish = finished_at if finished_at.tzinfo else finished_at.replace(tzinfo=timezone.utc)
    seconds = (finish.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    return seconds if seconds >= 0 else None


def _pipeline_url_from_job_url(job_url, pipeline_id):
    raw = (job_url or "").strip()
    marker = "/-/jobs/"
    if not raw or marker not in raw or not pipeline_id:
        return None
    repo_url = raw.split(marker, 1)[0].rstrip("/")
    return f"{repo_url}/-/pipelines/{pipeline_id}" if repo_url else None


def _as_utc_datetime(value):
    if not value:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def backfill_duration_columns(engine) -> None:
    """Fill duration_seconds / fix pipeline URLs on legacy rows. Idempotent."""
    from .models import ActivePipeline, JobReport, JobReportDetail, PipelineJob

    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        updated_reports = 0
        updated_report_urls = 0
        report_rows = (
            db.query(JobReport, JobReportDetail)
            .join(JobReportDetail, JobReportDetail.job_report_id == JobReport.id)
            .all()
        )
        for report, detail in report_rows:
            payload = detail.raw_payload or {}
            changed = False
            if report.duration_seconds is None:
                duration = _parse_duration_seconds(payload.get("duration_seconds"))
                if duration is None:
                    duration = _duration_between_seconds(
                        _parse_datetime(payload.get("started_at")),
                        _parse_datetime(payload.get("finished_at")) or report.finished_at,
                    )
                if duration is not None:
                    report.duration_seconds = duration
                    updated_reports += 1
                    changed = True

            fixed_pipeline_url = _pipeline_url_from_job_url(
                report.pipeline_url,
                payload.get("pipeline_id"),
            )
            if fixed_pipeline_url and fixed_pipeline_url != report.pipeline_url:
                report.pipeline_url = fixed_pipeline_url
                updated_report_urls += 1
                changed = True

            if changed:
                db.add(report)

        updated_jobs = 0
        for job in db.query(PipelineJob).filter(PipelineJob.duration_seconds.is_(None)).all():
            duration = _duration_between_seconds(job.started_at, job.finished_at)
            if duration is not None:
                job.duration_seconds = duration
                db.add(job)
                updated_jobs += 1

        updated_pipelines = 0
        for pipeline in db.query(ActivePipeline).filter(ActivePipeline.duration_seconds.is_(None)).all():
            jobs = db.query(PipelineJob).filter(PipelineJob.active_pipeline_id == pipeline.id).all()
            started_values = [_as_utc_datetime(job.started_at) for job in jobs if job.started_at]
            finished_values = [_as_utc_datetime(job.finished_at) for job in jobs if job.finished_at]
            if started_values:
                pipeline.started_at = min(started_values)
            if finished_values:
                pipeline.finished_at = max(finished_values)
            duration = _duration_between_seconds(pipeline.started_at, pipeline.finished_at)
            if duration is not None:
                pipeline.duration_seconds = duration
                db.add(pipeline)
                updated_pipelines += 1

        if updated_reports or updated_report_urls or updated_jobs or updated_pipelines:
            db.commit()
            logger.info(
                "Duration backfill completed: job_reports=%s job_report_urls=%s pipeline_jobs=%s active_pipelines=%s",
                updated_reports,
                updated_report_urls,
                updated_jobs,
                updated_pipelines,
            )
    finally:
        db.close()
