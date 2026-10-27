"""Detect long-running Secpipe-related pipeline jobs and send alerts."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import ActivePipeline, PipelineJob, Product, Repository, SessionLocal
from ..models.pipeline import PipelineStatus
from .notifications import notify_long_running_job
from ..integration_settings import runtime_setting

logger = logging.getLogger(__name__)

_IN_PROGRESS_STATUSES = frozenset({"pending", "running", "created"})


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


_BUILTIN_SECPIPE_JOB_NAMES = (
    "semgrep_scan,sca_scan,gitleaks_scan,trivy_scan,asoc_scan,mobsf_scan"
)
_DEFAULT_EXCLUDED_JOB_NAMES = "ptai_scan"


def load_default_secpipe_job_names() -> Set[str]:
    """Job names for long-running alerts when regex is unset and pipeline has no tracked_jobs."""
    raw = (os.getenv("SECPIPE_JOB_LONG_ALERT_NAMES") or "").strip()
    if not raw:
        raw = (os.getenv("AUDIT_SECURITY_JOB_NAMES") or "").strip()
    if not raw:
        raw = _BUILTIN_SECPIPE_JOB_NAMES
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def load_excluded_secpipe_job_names() -> Set[str]:
    raw = os.getenv("SECPIPE_JOB_LONG_ALERT_EXCLUDE_NAMES", _DEFAULT_EXCLUDED_JOB_NAMES)
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def job_matches_secpipe_filter(
    job_name: str,
    tracked_jobs: Any,
    regex_pattern: Optional[str],
    default_names: Set[str],
) -> bool:
    n = (job_name or "").strip().lower()
    if not n:
        return False
    if n in load_excluded_secpipe_job_names():
        return False
    if regex_pattern:
        try:
            return bool(re.search(regex_pattern, job_name, re.IGNORECASE))
        except re.error:
            logger.warning("Invalid SECPIPE_JOB_LONG_ALERT_REGEX, ignoring")
            return False
    in_defaults = n in default_names
    if isinstance(tracked_jobs, list) and tracked_jobs:
        tracked = {str(t).strip().lower() for t in tracked_jobs if str(t).strip()}
        if tracked:
            return in_defaults or n in tracked
    return in_defaults


def find_long_running_pipeline_jobs(
    db: Session,
    *,
    threshold_minutes: int,
    resend_minutes: int,
    regex_pattern: Optional[str],
    default_names: Set[str],
) -> List[Tuple[PipelineJob, ActivePipeline, Repository, Product]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=threshold_minutes)
    try:
        poll_interval_seconds = max(0, int(os.getenv("PIPELINE_POLL_INTERVAL_SECONDS", "300")))
    except ValueError:
        poll_interval_seconds = 300
    # Job status is only trustworthy while the pipeline poller has observed
    # that exact GitLab job recently. Give the poller at least 15 minutes (or
    # three configured poll intervals) to recover before suppressing alerts.
    observation_cutoff = now - timedelta(seconds=max(15 * 60, poll_interval_seconds * 3))
    status_lower = func.lower(PipelineJob.status)
    query = (
        db.query(PipelineJob, ActivePipeline, Repository, Product)
        .join(ActivePipeline, ActivePipeline.id == PipelineJob.active_pipeline_id)
        .join(Repository, Repository.id == ActivePipeline.repository_id)
        .join(Product, Product.id == ActivePipeline.product_id)
        # A pipeline that stopped being polled must not keep producing alerts
        # from its last observed (possibly stale) running job status.
        .filter(ActivePipeline.status.in_((PipelineStatus.REGISTERED, PipelineStatus.POLLING)))
        .filter(status_lower.in_(tuple(_IN_PROGRESS_STATUSES)))
        .filter(PipelineJob.started_at.isnot(None))
        .filter(PipelineJob.started_at <= cutoff)
        .filter(PipelineJob.last_seen_at >= observation_cutoff)
    )
    if resend_minutes > 0:
        # Re-alert mode: send again if last alert was more than resend_minutes ago.
        resend_cutoff = now - timedelta(minutes=resend_minutes)
        query = query.filter(
            or_(
                PipelineJob.last_long_running_alert_at.is_(None),
                PipelineJob.last_long_running_alert_at <= resend_cutoff,
            )
        )
    else:
        # One-shot mode (default): alert exactly once when the threshold is
        # crossed; never re-alert for the same job. A hung 3-day job no longer
        # spams the channel.
        query = query.filter(PipelineJob.last_long_running_alert_at.is_(None))
    rows = query.all()
    out: List[Tuple[PipelineJob, ActivePipeline, Repository, Product]] = []
    for pj, ap, repo, prod in rows:
        if job_matches_secpipe_filter(pj.name, ap.tracked_jobs, regex_pattern, default_names):
            out.append((pj, ap, repo, prod))
    if len(rows) != len(out):
        logger.info(
            "Long-running job scan: %s row(s) past threshold, %s after name filter (defaults=%s)",
            len(rows),
            len(out),
            len(default_names),
        )
    return out


async def run_long_running_job_alert_cycle(db: Session) -> int:
    if not _bool_env("SECPIPE_JOB_LONG_ALERT_ENABLED", True):
        logger.info("job_duration_cycle_skipped reason=disabled")
        return 0
    has_mm = bool(runtime_setting("MATTERMOST_WEBHOOK_URL", "").strip())
    has_mail = bool((os.getenv("ALERT_EMAIL_TO") or "").strip() and (os.getenv("ALERT_SMTP_HOST") or "").strip())
    if not has_mm and not has_mail:
        logger.warning(
            "Job duration alerts enabled but no channel configured "
            "(connect Mattermost in Integrations and/or configure email alerts)"
        )
        return 0

    threshold = int(os.getenv("SECPIPE_JOB_LONG_ALERT_MINUTES", "45"))
    resend = int(os.getenv("SECPIPE_JOB_ALERT_RESEND_MINUTES", "60"))
    regex = (os.getenv("SECPIPE_JOB_LONG_ALERT_REGEX") or "").strip() or None
    default_names = load_default_secpipe_job_names()
    logger.info(
        "job_duration_cycle_start threshold_minutes=%s resend_minutes=%s regex=%s default_names=%s excluded_names=%s",
        threshold,
        resend,
        regex or "",
        sorted(default_names),
        sorted(load_excluded_secpipe_job_names()),
    )
    if not default_names and not regex:
        logger.warning(
            "Job duration alerts: no job names to match "
            "(SECPIPE_JOB_LONG_ALERT_NAMES empty and SECPIPE_JOB_LONG_ALERT_REGEX unset)"
        )
    rows = find_long_running_pipeline_jobs(
        db,
        threshold_minutes=threshold,
        resend_minutes=resend,
        regex_pattern=regex,
        default_names=default_names,
    )
    now = datetime.now(timezone.utc)
    sent = 0
    logger.info("job_duration_candidates count=%s", len(rows))
    for pj, ap, repo, prod in rows:
        started = pj.started_at
        if started is None:
            continue
        started_utc = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
        elapsed_min = (now - started_utc.astimezone(timezone.utc)).total_seconds() / 60.0
        text = (
            f"**ScanOps job long-running** (~{elapsed_min:.0f} min since start)\n"
            f"- **Product:** {prod.name}\n"
            f"- **Repository:** `{repo.path}`\n"
            f"- **Job:** `{pj.name}` — status `{pj.status}`\n"
            f"- **Pipeline:** #{ap.pipeline_id} (GitLab project_id={ap.project_id})\n"
        )
        if pj.web_url:
            text += f"- **Job URL:** {pj.web_url}\n"
        if ap.web_url:
            text += f"- **Pipeline URL:** {ap.web_url}\n"
        subject = f"[ScanOps] Long-running job: {pj.name} ({repo.path})"
        ok = await notify_long_running_job(text=text, subject=subject)
        if ok:
            pj.last_long_running_alert_at = now
            db.add(pj)
            sent += 1
            logger.info(
                "job_duration_alert_sent job_id=%s job=%s status=%s active_pipeline_id=%s project_id=%s pipeline_id=%s repo=%s elapsed_min=%.1f",
                pj.gitlab_job_id,
                pj.name,
                pj.status,
                ap.id,
                ap.project_id,
                ap.pipeline_id,
                repo.path,
                elapsed_min,
            )
        else:
            logger.warning(
                "job_duration_alert_delivery_failed job_id=%s job=%s active_pipeline_id=%s project_id=%s pipeline_id=%s",
                pj.gitlab_job_id,
                pj.name,
                ap.id,
                ap.project_id,
                ap.pipeline_id,
            )
    if sent:
        db.commit()
    elif rows:
        logger.warning(
            "job_duration_cycle_finish candidates=%s sent=0 reason=delivery_failed",
            len(rows),
        )
    else:
        logger.info("job_duration_cycle_finish candidates=0 sent=0")
    return sent


class JobDurationAlertPoller:
    """Background loop: scan DB for long-running pipeline jobs and notify."""

    def __init__(self) -> None:
        self.interval_seconds = int(os.getenv("SECPIPE_JOB_ALERT_INTERVAL_SECONDS", "300"))
        self._task: asyncio.Task | None = None  # type: ignore[name-defined]

    async def start(self) -> None:
        if self.interval_seconds <= 0:
            logger.info("Job duration alert poller disabled (SECPIPE_JOB_ALERT_INTERVAL_SECONDS<=0)")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        logger.info("Job duration alert poller started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Job duration alert poller stopped")

    async def _run(self) -> None:
        while True:
            try:
                with SessionLocal() as db:
                    n = await run_long_running_job_alert_cycle(db)
                    if n:
                        logger.info("Sent %s long-running job alert(s)", n)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Job duration alert cycle failed")
            await asyncio.sleep(self.interval_seconds)
