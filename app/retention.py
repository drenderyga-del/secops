"""Data retention: prune old rows so the DB does not grow forever.

Called daily from the pipeline worker. Controlled by SECPIPE_RETENTION_DAYS
(0 or unset = disabled in code; docker-compose sets a sane default).

What is pruned (children removed via DB-level ON DELETE CASCADE):

- job_reports (+ job_report_details) older than the cutoff;
- active_pipelines (+ pipeline_jobs) finished/failed/stale older than cutoff —
  pipelines still in polling state are never touched;
- config_scan_runs (+ config_scan_findings) older than cutoff;
- iac_scan_runs (+ iac repositories/findings) older than cutoff;
- audit_scan_runs older than cutoff (snapshots keep history; runs are noise).

Latest config/iac run per scope is preserved even if old, so overview pages
never go empty after cleanup.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


def retention_days() -> int:
    try:
        return int(os.getenv("SECPIPE_RETENTION_DAYS", "0"))
    except ValueError:
        return 0


_STATEMENTS = [
    (
        "job_reports",
        "DELETE FROM job_reports WHERE finished_at < :cutoff",
    ),
    (
        "active_pipelines",
        """
        -- SQLAlchemy Enum(native_enum=False) stores enum NAMES, hence uppercase.
        DELETE FROM active_pipelines
        WHERE status IN ('COMPLETED', 'FAILED', 'STALE')
          AND COALESCE(finished_at, created_at) < :cutoff
        """,
    ),
    (
        "config_scan_runs",
        """
        DELETE FROM config_scan_runs
        WHERE created_at < :cutoff
          AND id NOT IN (
            SELECT MAX(id) FROM config_scan_runs GROUP BY product_id
          )
        """,
    ),
    (
        "iac_scan_runs",
        """
        DELETE FROM iac_scan_runs
        WHERE created_at < :cutoff
          AND status NOT IN ('queued', 'running')
          AND cancel_requested_at IS NULL
          AND (
            single_project_id IS NOT NULL
            OR id NOT IN (
              SELECT MAX(id)
              FROM iac_scan_runs
              WHERE single_project_id IS NULL
                AND status NOT IN ('queued', 'running')
              GROUP BY gitlab_base_url, scope_path
            )
          )
        """,
    ),
    (
        "audit_scan_runs",
        "DELETE FROM audit_scan_runs WHERE created_at < :cutoff",
    ),
]


def cleanup_once(engine) -> dict[str, int]:
    """Run one retention pass. Returns rows deleted per table."""
    days = retention_days()
    if days <= 0:
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted: dict[str, int] = {}
    for table, sql in _STATEMENTS:
        try:
            with engine.begin() as conn:
                result = conn.execute(text(sql), {"cutoff": cutoff})
                count = result.rowcount or 0
        except Exception:
            logger.exception("Retention cleanup failed for %s", table)
            continue
        if count:
            deleted[table] = count
    if deleted:
        logger.info("Retention cleanup (older than %sd): %s", days, deleted)
    return deleted
