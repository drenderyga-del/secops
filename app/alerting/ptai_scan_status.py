"""Poll ASOC for the real status of async PT AI scans and alert on failures.

The GitLab `ptai_scan` job only triggers the scan (fire-and-forget), so its
duration says nothing about the scan itself. ``pipeline_tracker`` captures the
ASOC ids from the finished job trace into ``ptai_scans``; this poller follows
each scan inside ASOC and alerts when it fails, hangs, or was never accepted.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..database import DatabaseService
from ..execution_contract import LegacySource
from ..execution_service import ExecutionService
from ..iac_asoc import AsocClient
from ..models import (
    PtaiScan,
    PTAI_ENGINE_FAILED_STAGES,
    PTAI_FAILED_STATUSES,
    PTAI_TERMINAL_STATUSES,
    SessionLocal,
)
from ..integration_settings import runtime_setting
from ..ptai_client import PtaiClient, ptai_result_url
from .notifications import notify_long_running_job

logger = logging.getLogger(__name__)


def _observe_shared_execution(db: Session, scan: PtaiScan, observed_at: datetime) -> None:
    ExecutionService(db).observe_legacy_execution(
        source=LegacySource.PTAI_ASOC,
        raw_status=scan.asoc_status,
        correlation_key=f"ptai_asoc:{scan.gitlab_job_id}",
        repository_id=scan.repository_id,
        product_id=scan.product_id,
        observed_at=scan.finished_at or observed_at,
    )

# BackgroundTaskStatus -> our ScanPipelineStatus space (used only as a fallback
# when no asoc_pipeline_id is available).
_TASK_STATUS_MAP = {
    "SUCCESS": "SUCCESS",
    "FAILED": "ERROR",
    "PENDING": "PENDING",
    "PROGRESS": "IN_PROGRESS",
    "RETRYING": "IN_PROGRESS",
    "LOCKED": "IN_PROGRESS",
    "SKIP": "SUCCESS",
}

_ASOC_STATUS_MAP = {
    "DONE": "SUCCESS",
    "SUCCESS": "SUCCESS",
    "SUCCEEDED": "SUCCESS",
    "SKIP": "SUCCESS",
    "SKIPPED": "SUCCESS",
    "PARTIAL_SUCCESS": "PARTIAL_SUCCESS",
    "PARTIALSUCCESS": "PARTIAL_SUCCESS",
    "PARTIAL": "PARTIAL_SUCCESS",
    "ERROR": "ERROR",
    "FAILED": "ERROR",
    "FAILURE": "ERROR",
    "ABORTED": "ERROR",
    "CANCELED": "ERROR",
    "CANCELLED": "ERROR",
    "PENDING": "PENDING",
    "QUEUED": "PENDING",
    "ENQUEUED": "PENDING",
    "PROGRESS": "IN_PROGRESS",
    "IN_PROGRESS": "IN_PROGRESS",
    "INPROGRESS": "IN_PROGRESS",
    "RUNNING": "IN_PROGRESS",
    "SCAN": "IN_PROGRESS",
    "LOCKED": "IN_PROGRESS",
    "RETRYING": "IN_PROGRESS",
}


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_asoc_status(raw_status: Optional[str]) -> Optional[str]:
    if raw_status is None:
        return None
    key = str(raw_status).strip().replace("-", "_").replace(" ", "_").upper()
    return _ASOC_STATUS_MAP.get(key, key or None)


def _channels_configured() -> bool:
    has_mm = bool(runtime_setting("MATTERMOST_WEBHOOK_URL", "").strip())
    has_mail = bool(
        (os.getenv("ALERT_EMAIL_TO") or "").strip()
        and (os.getenv("ALERT_SMTP_HOST") or "").strip()
    )
    return has_mm or has_mail


def _scan_label(scan: PtaiScan) -> str:
    repo = scan.repo_slug or (scan.repository.path if scan.repository else "?")
    return f"{repo} @ {scan.branch or '?'}"


def _asoc_repository_id(payload: Optional[dict]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    repository = payload.get("repository")
    if isinstance(repository, dict) and repository.get("id"):
        return str(repository["id"])
    for key in ("repositoryID", "repositoryId", "repository_id"):
        if payload.get(key):
            return str(payload[key])
    return None


def resolve_asoc_repository_id(client: AsocClient, scan: PtaiScan) -> Optional[str]:
    """Best-effort fallback for scans captured before repository_id was logged."""
    if not scan.repo_slug:
        return None
    repository = client.find_repository(scan.repo_slug)
    return str(repository.get("id")) if repository and repository.get("id") else None


def asoc_public_url(base_url: Optional[str] = None) -> Optional[str]:
    from ..asoc_description_safety import safe_asoc_public_base

    api_url = (base_url if base_url is not None else runtime_setting("HEXWAY_URL", "")).strip().rstrip("/")
    return safe_asoc_public_base(api_url)


def asoc_repository_url(scan: PtaiScan, base_url: Optional[str] = None) -> Optional[str]:
    """ASOC repository URL.

    The ``id`` returned by ``POST /ext/v1/scan/`` is a Vampy orchestration
    request id, not the repository scan id used by the ASOC UI. Building
    ``/repositories/{repository}/scans/{id}`` from it produces a dead link.
    """
    base_url = asoc_public_url(base_url)
    repository_id = getattr(scan, "asoc_repository_id", None)
    if not base_url or not repository_id:
        return None
    return f"{base_url}/repositories/{repository_id}"


def scan_monitor_url(scan: PtaiScan) -> Optional[str]:
    base_url = (os.getenv("SECPIPE_PUBLIC_URL") or "").strip().rstrip("/")
    scan_id = getattr(scan, "id", None)
    if not base_url or scan_id is None:
        return None
    return f"{base_url}/ptai-scans/{scan_id}"


def _alert_links(scan: PtaiScan) -> str:
    lines = []
    if scan.job_web_url:
        lines.append(f"- **GitLab job:** [open job]({scan.job_web_url})")
    monitor_url = scan_monitor_url(scan)
    if monitor_url:
        lines.append(f"- **Monitoring:** [PT AI scan #{scan.id}]({monitor_url})")
    engine_url = ptai_result_url(scan)
    if engine_url:
        lines.append(f"- **PT AI result:** [open result]({engine_url})")
    asoc_url = asoc_repository_url(scan)
    asoc_ref = scan.asoc_pipeline_id or scan.asoc_task_id
    if asoc_url:
        suffix = f"; pipeline `{asoc_ref}`" if asoc_ref else ""
        lines.append(f"- **ASOC repository:** [open repository]({asoc_url}){suffix}")
    elif asoc_ref:
        lines.append(f"- **ASOC pipeline:** `{asoc_ref}` (direct scan link is unavailable: repository or scan ID was not captured)")
    return "\n".join(lines) + ("\n" if lines else "")


def build_failed_scan_alert_text(scan: PtaiScan, status: str, detail: Optional[str]) -> str:
    engine_stage = (scan.ptai_stage or "").strip()
    engine_failed = engine_stage in PTAI_ENGINE_FAILED_STAGES
    engine_succeeded = engine_stage == "Done" and not scan.ptai_has_errors

    if engine_failed:
        title = f"**PT AI scanner failed** — ASOC status `{status}`"
        explanation = (
            "The GitLab job only started the asynchronous scan; "
            "the failure happened later in the PT AI engine."
        )
    elif engine_succeeded:
        title = f"**ASOC processing failed after PT AI completed** — status `{status}`"
        explanation = (
            "PT AI reports the scan as completed, but ASOC did not finish "
            "or confirm the orchestration successfully."
        )
    else:
        title = f"**PT AI scan failed in ASOC** — status `{status}`"
        explanation = (
            "The GitLab job only submitted the asynchronous scan and may be successful; "
            "ASOC reported the later scan failure."
        )

    text = (
        f"{title}\n"
        f"{explanation}\n"
        f"- **Target:** `{_scan_label(scan)}`\n"
    )
    if detail:
        text += f"- **ASOC error:** {detail}\n"
    if engine_stage or scan.ptai_error:
        text += f"- **PT AI engine:** stage `{engine_stage or '?'}`"
        if scan.ptai_error:
            text += f" — {scan.ptai_error}"
        text += "\n"
    return text + _alert_links(scan)


def fetch_status_by_ids(client: AsocClient, pipeline_id: Optional[str], task_id: Optional[str]) -> Optional[dict]:
    """Thread-safe variant of fetch_status: plain ids in, plain dict out."""

    class _Ids:
        asoc_pipeline_id = pipeline_id
        asoc_task_id = task_id

    return fetch_status(client, _Ids())


def fetch_status(client: AsocClient, scan) -> Optional[dict]:
    """Resolve a scan's status via pipeline endpoint, falling back to task."""
    if scan.asoc_pipeline_id:
        body = client.get_scan_pipeline_status(scan.asoc_pipeline_id)
        if body and body.get("status"):
            raw_status = body.get("status")
            return {
                "source": "asoc_pipeline",
                "raw_status": raw_status,
                "status": normalize_asoc_status(raw_status),
                "finished": body.get("finished"),
                "created": body.get("created"),
                "asoc_repository_id": _asoc_repository_id(body),
            }
    if scan.asoc_task_id:
        task = client.get_background_task(scan.asoc_task_id)
        if task and task.get("status"):
            raw_status = task["status"]
            return {
                "source": "asoc_task",
                "raw_status": raw_status,
                "status": normalize_asoc_status(_TASK_STATUS_MAP.get(raw_status, raw_status)),
                "finished": task.get("updated") if task["status"] in {"SUCCESS", "FAILED", "SKIP"} else None,
                "created": task.get("created"),
                "errorDetails": task.get("errorDetails"),
                "asoc_repository_id": _asoc_repository_id(task),
            }
    return None


def _resolve_engine_status(client: PtaiClient, *, repo_slug, branch, project_id, branch_id, scan_id):
    """HTTP part of the engine enrichment, safe to run in a worker thread.

    Takes/returns plain values only — session-bound ORM instances must not be
    touched from another thread (SQLAlchemy sessions are not thread-safe), so
    the caller reads the arguments and applies the returned DTO in its own
    thread. Never raises.
    """
    try:
        return client.resolve_status(
            repo_slug=repo_slug, branch=branch, project_id=project_id, branch_id=branch_id
        )
    except Exception:
        logger.exception("PT AI engine resolve failed for scan %s", scan_id)
        return None


def _apply_engine_fields(scan: PtaiScan, status, now: datetime) -> None:
    scan.ptai_project_id = status.project_id
    scan.ptai_branch_id = status.branch_id
    scan.ptai_scan_result_id = status.scan_result_id
    scan.ptai_stage = status.stage
    scan.ptai_progress = status.progress
    scan.ptai_has_errors = status.has_errors
    scan.ptai_error = status.error
    scan.ptai_polled_at = now


def enrich_ptai_engine(client: PtaiClient, scan: PtaiScan, now: datetime) -> None:
    """Best-effort: fill PT AI engine fields on the row. Never raises.

    Synchronous variant for callers in the session's own thread; async code
    must thread only ``_resolve_engine_status`` and apply fields itself.
    """
    if not scan.repo_slug:
        return
    status = _resolve_engine_status(
        client,
        repo_slug=scan.repo_slug,
        branch=scan.branch,
        project_id=scan.ptai_project_id,
        branch_id=scan.ptai_branch_id,
        scan_id=scan.id,
    )
    if status is None:
        return
    _apply_engine_fields(scan, status, now)


def status_from_ptai_engine(scan: PtaiScan) -> Optional[str]:
    """Map direct PT AI engine stage to the UI/alert status space.

    ASOC scan-pipeline status is preferred when available, but in practice the
    external status endpoint may be inaccessible while PT AI already knows the
    real stage. This keeps rows from staying visually PENDING forever.
    """
    stage = (scan.ptai_stage or "").strip()
    if not stage:
        return None
    if stage == "Done":
        return "PARTIAL_SUCCESS" if scan.ptai_has_errors else "SUCCESS"
    if stage in PTAI_ENGINE_FAILED_STAGES:
        return "ERROR"
    return "IN_PROGRESS"


def _apply_engine_status(scan: PtaiScan, now: datetime) -> bool:
    engine_status = status_from_ptai_engine(scan)
    if not engine_status:
        return False
    scan.asoc_status = engine_status
    if engine_status in PTAI_TERMINAL_STATUSES and not scan.finished_at:
        scan.finished_at = scan.ptai_polled_at or now
    if engine_status in PTAI_FAILED_STATUSES:
        scan.error_text = scan.ptai_error or f"PT AI engine returned terminal stage {scan.ptai_stage}"
    elif engine_status in PTAI_TERMINAL_STATUSES:
        scan.error_text = None
    elif scan.error_text and scan.error_text.startswith(("ASOC poll skipped:", "ASOC status unavailable")):
        scan.error_text = None
    return True


def poll_one_scan(db: Session, scan_id: int) -> dict:
    """Poll ASOC (and the PT AI engine) for ONE scan right now, no alerts.

    Backs the "Refresh status now" button on the scan detail page and
    returns a debug dict with the raw ASOC responses (code + body) so an
    operator can see exactly what ASOC reports for a scan that looks stuck.
    Synchronous (blocking requests) — call from a sync route/threadpool.
    """
    scan = db.query(PtaiScan).filter(PtaiScan.id == scan_id).first()
    if not scan:
        return {"error": "scan not found"}

    now = datetime.now(timezone.utc)
    debug: dict = {"asoc_requests": [], "engine": None, "applied": None}

    ptai_client = PtaiClient()
    if ptai_client.enabled() and scan.repo_slug:
        enrich_ptai_engine(ptai_client, scan, now)
        debug["engine"] = {
            "stage": scan.ptai_stage,
            "progress": scan.ptai_progress,
            "has_errors": scan.ptai_has_errors,
            "error": scan.ptai_error,
        }

    client = AsocClient()
    debug["asoc_enabled"] = client.enabled()
    scan.last_polled_at = now
    if not client.enabled():
        if not _apply_engine_status(scan, now):
            scan.error_text = "ASOC poll skipped: connector is not configured in Integrations"
            debug["error"] = scan.error_text
        else:
            debug["note"] = "ASOC poll skipped, status applied from PT AI engine."
        db.add(scan)
        _observe_shared_execution(db, scan, now)
        db.commit()
        db.refresh(scan)
        debug["applied"] = {
            "asoc_status": scan.asoc_status,
            "error_text": scan.error_text,
            "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
        }
        return debug

    # Raw responses for the debug panel (surface 404/403/body, not just None).
    if scan.asoc_pipeline_id:
        debug["asoc_requests"].append(
            {"label": "pipeline status", **client.debug_get(f"/ext/v1/scan/pipelines/{scan.asoc_pipeline_id}/status/")}
        )
    if scan.asoc_task_id:
        debug["asoc_requests"].append(
            {"label": "background task", **client.debug_get(f"/ext/v1/background_tasks/{scan.asoc_task_id}/")}
        )
    if not scan.asoc_pipeline_id and not scan.asoc_task_id:
        debug["note"] = "No ASOC pipeline_id or task_id captured for this scan — nothing to poll."

    status = fetch_status(client, scan)
    if status is None:
        if not _apply_engine_status(scan, now):
            ref = scan.asoc_pipeline_id or scan.asoc_task_id or "missing ASOC id"
            scan.error_text = f"ASOC status unavailable for {ref}"
    else:
        new_status = status["status"]
        if not getattr(scan, "asoc_repository_id", None):
            scan.asoc_repository_id = status.get("asoc_repository_id") or resolve_asoc_repository_id(client, scan)
        scan.asoc_status = new_status
        finished = _parse_iso(status.get("finished"))
        if finished:
            scan.finished_at = finished
        detail = status.get("errorDetails")
        if new_status in PTAI_FAILED_STATUSES and not detail and scan.asoc_task_id:
            task = client.get_background_task(scan.asoc_task_id)
            detail = (task or {}).get("errorDetails")
        if new_status in PTAI_FAILED_STATUSES:
            scan.error_text = detail or f"ASOC returned terminal status {new_status}"
        elif new_status in PTAI_TERMINAL_STATUSES:
            scan.error_text = None
        elif scan.error_text and scan.error_text.startswith(("ASOC poll skipped:", "ASOC status unavailable")):
            scan.error_text = None

    db.add(scan)
    _observe_shared_execution(db, scan, now)
    db.commit()
    db.refresh(scan)
    debug["applied"] = {
        "asoc_status": scan.asoc_status,
        "error_text": scan.error_text,
        "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
    }
    return debug


async def run_ptai_scan_status_cycle(db: Session) -> int:
    alerts_enabled = _bool_env("PTAI_SCAN_STATUS_ALERT_ENABLED", True)
    if alerts_enabled and not _channels_configured():
        logger.warning(
            "PT AI scan status alerts enabled but no channel configured; "
            "status polling will continue without notifications "
            "(connect Mattermost in Integrations and/or configure email alerts)"
        )
        alerts_enabled = False

    hung_minutes = int(os.getenv("PTAI_SCAN_HUNG_ALERT_MINUTES", "120"))
    batch = int(os.getenv("PTAI_SCAN_STATUS_BATCH_SIZE", "50"))
    now = datetime.now(timezone.utc)
    service = DatabaseService(db)
    sent = 0

    # Optional PT AI engine enrichment (project name `vampy-asoc-<slug>`).
    ptai_client = PtaiClient()
    ptai_enabled = ptai_client.enabled()

    # 1. Scans that were never accepted by ASOC (no ids in trace).
    for scan in (
        db.query(PtaiScan)
        .filter(PtaiScan.trigger_failed.is_(True))
        .filter(PtaiScan.last_alert_at.is_(None))
        .limit(batch)
        .all()
    ):
        if not alerts_enabled:
            continue
        text = (
            "**PT AI scan trigger failed** — ASOC did not accept the scan\n"
            "The GitLab job could not obtain ASOC correlation IDs, so no asynchronous scan can be tracked.\n"
            f"- **Target:** `{_scan_label(scan)}`\n"
        )
        text += _alert_links(scan)
        if await notify_long_running_job(text=text, subject=f"[ScanOps] PT AI scan trigger failed: {_scan_label(scan)}"):
            scan.last_alert_at = now
            db.add(scan)
            _observe_shared_execution(db, scan, now)
            sent += 1

    client = AsocClient()
    logger.info(
        "ptai_status_cycle_start asoc_enabled=%s ptai_enabled=%s batch=%s alerts_enabled=%s",
        client.enabled(),
        ptai_enabled,
        batch,
        alerts_enabled,
    )
    if not client.enabled():
        logger.warning("PT AI scan status poll skipped: HEXWAY_URL/HEXWAY_TOKEN not configured")
        pending_scans = service.fetch_pending_ptai_scans(limit=batch)
        logger.info("ptai_status_cycle_selected count=%s mode=ptai-only", len(pending_scans))
        for scan in pending_scans:
            scan.last_polled_at = now
            logger.info(
                "ptai_status_scan_start id=%s repo=%s branch=%s old_status=%s pipeline_id=%s task_id=%s mode=ptai-only",
                scan.id,
                scan.repo_slug,
                scan.branch,
                scan.asoc_status,
                scan.asoc_pipeline_id,
                scan.asoc_task_id,
            )
            if ptai_enabled:
                await asyncio.to_thread(enrich_ptai_engine, ptai_client, scan, now)
                logger.info(
                    "ptai_status_engine_result id=%s stage=%s progress=%s has_errors=%s project_id=%s branch_id=%s",
                    scan.id,
                    scan.ptai_stage,
                    scan.ptai_progress,
                    scan.ptai_has_errors,
                    scan.ptai_project_id,
                    scan.ptai_branch_id,
                )
            if not _apply_engine_status(scan, now):
                scan.error_text = "ASOC poll skipped: connector is not configured in Integrations"
                logger.warning(
                    "ptai_status_scan_unresolved id=%s reason=asoc-disabled-and-no-ptai-engine-status",
                    scan.id,
                )
            else:
                logger.info(
                    "ptai_status_scan_applied id=%s source=ptai_engine new_status=%s finished_at=%s error=%s",
                    scan.id,
                    scan.asoc_status,
                    scan.finished_at,
                    scan.error_text,
                )
            if alerts_enabled and scan.asoc_status in PTAI_FAILED_STATUSES and scan.last_alert_at is None:
                text = (
                    f"**PT AI scanner failed** — engine status `{scan.asoc_status}`\n"
                    "The GitLab job only started the asynchronous scan; the failure happened later in PT AI.\n"
                    f"- **Target:** `{_scan_label(scan)}`\n"
                )
                if scan.ptai_error:
                    text += f"- **PT AI error:** {scan.ptai_error}\n"
                text += _alert_links(scan)
                if await notify_long_running_job(text=text, subject=f"[ScanOps] PT AI scan {scan.asoc_status}: {_scan_label(scan)}"):
                    scan.last_alert_at = now
                    sent += 1
            db.add(scan)
            _observe_shared_execution(db, scan, now)
        db.commit()
        logger.info("ptai_status_cycle_finish processed=%s alerts_sent=%s mode=ptai-only", len(pending_scans), sent)
        return sent

    # 2. Live scans — poll ASOC for the real outcome.
    pending_scans = service.fetch_pending_ptai_scans(limit=batch)
    logger.info("ptai_status_cycle_selected count=%s mode=asoc", len(pending_scans))
    for scan in pending_scans:
        old_status = scan.asoc_status
        logger.info(
            "ptai_status_scan_start id=%s repo=%s branch=%s old_status=%s pipeline_id=%s task_id=%s",
            scan.id,
            scan.repo_slug,
            scan.branch,
            scan.asoc_status,
            scan.asoc_pipeline_id,
            scan.asoc_task_id,
        )
        if ptai_enabled and scan.repo_slug:
            # Only the HTTP call runs in the thread; ORM fields are read and
            # written here, in the session's own thread (see _resolve_engine_status).
            engine_status = await asyncio.to_thread(
                _resolve_engine_status,
                ptai_client,
                repo_slug=scan.repo_slug,
                branch=scan.branch,
                project_id=scan.ptai_project_id,
                branch_id=scan.ptai_branch_id,
                scan_id=scan.id,
            )
            if engine_status is not None:
                _apply_engine_fields(scan, engine_status, now)
            logger.info(
                "ptai_status_engine_result id=%s stage=%s progress=%s has_errors=%s project_id=%s branch_id=%s",
                scan.id,
                scan.ptai_stage,
                scan.ptai_progress,
                scan.ptai_has_errors,
                scan.ptai_project_id,
                scan.ptai_branch_id,
            )
        status = await asyncio.to_thread(
            fetch_status_by_ids, client, scan.asoc_pipeline_id, scan.asoc_task_id
        )
        scan.last_polled_at = now
        if status is None:
            if not _apply_engine_status(scan, now):
                ref = scan.asoc_pipeline_id or scan.asoc_task_id or "missing ASOC id"
                scan.error_text = f"ASOC status unavailable for {ref}"
                logger.warning(
                    "ptai_status_scan_unresolved id=%s ref=%s old_status=%s error=%s",
                    scan.id,
                    ref,
                    old_status,
                    scan.error_text,
                )
                db.add(scan)
                continue
            new_status = scan.asoc_status
            created = scan.created_at
            detail = scan.error_text
            logger.info(
                "ptai_status_scan_applied id=%s source=ptai_engine old_status=%s new_status=%s stage=%s",
                scan.id,
                old_status,
                new_status,
                scan.ptai_stage,
            )
        else:
            new_status = status["status"]
            if not getattr(scan, "asoc_repository_id", None):
                scan.asoc_repository_id = (
                    status.get("asoc_repository_id")
                    or await asyncio.to_thread(resolve_asoc_repository_id, client, scan)
                )
            scan.asoc_status = new_status
            finished = _parse_iso(status.get("finished"))
            created = _parse_iso(status.get("created"))
            if finished:
                scan.finished_at = finished

            detail = status.get("errorDetails")
            if new_status in PTAI_FAILED_STATUSES and not detail and scan.asoc_task_id:
                task = await asyncio.to_thread(client.get_background_task, scan.asoc_task_id)
                detail = (task or {}).get("errorDetails")

            if new_status in PTAI_FAILED_STATUSES:
                scan.error_text = detail or f"ASOC returned terminal status {new_status}"
            elif new_status in PTAI_TERMINAL_STATUSES:
                scan.error_text = None
            elif scan.error_text and scan.error_text.startswith(("ASOC poll skipped:", "ASOC status unavailable")):
                scan.error_text = None
            logger.info(
                "ptai_status_scan_applied id=%s source=%s raw_status=%s old_status=%s new_status=%s finished_at=%s error=%s",
                scan.id,
                status.get("source"),
                status.get("raw_status"),
                old_status,
                new_status,
                scan.finished_at,
                scan.error_text,
            )

        # Failure alert fires on the TRANSITION into a failed status, not on
        # `last_alert_at is None`: a hung-scan alert also sets last_alert_at,
        # which used to permanently suppress the later, more important
        # ERROR/PARTIAL_SUCCESS alert for the same scan.
        became_failed = new_status in PTAI_FAILED_STATUSES and old_status not in PTAI_FAILED_STATUSES
        if alerts_enabled and became_failed:
            text = build_failed_scan_alert_text(scan, new_status, detail)
            if await notify_long_running_job(text=text, subject=f"[ScanOps] PT AI scan {new_status}: {_scan_label(scan)}"):
                scan.last_alert_at = now
                sent += 1
        elif alerts_enabled and new_status not in PTAI_TERMINAL_STATUSES and scan.last_alert_at is None:
            started = created or scan.created_at
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started:
                elapsed_min = (now - started).total_seconds() / 60.0
                if elapsed_min >= hung_minutes:
                    text = (
                        f"**PT AI scan still running** (~{elapsed_min:.0f} min, status `{new_status}`)\n"
                        "The GitLab trigger job may already be successful; the asynchronous scan is still running in ASOC/PT AI.\n"
                        f"- **Target:** `{_scan_label(scan)}`\n"
                    )
                    text += _alert_links(scan)
                    if await notify_long_running_job(text=text, subject=f"[ScanOps] PT AI scan hung: {_scan_label(scan)}"):
                        scan.last_alert_at = now
                        sent += 1

        db.add(scan)
        _observe_shared_execution(db, scan, now)

    db.commit()
    logger.info("ptai_status_cycle_finish processed=%s alerts_sent=%s mode=asoc", len(pending_scans), sent)
    return sent


class PtaiScanStatusPoller:
    """Background loop: poll ASOC for async PT AI scan outcomes and alert."""

    def __init__(self) -> None:
        self.interval_seconds = int(os.getenv("PTAI_SCAN_STATUS_POLL_INTERVAL_SECONDS", "300"))
        self._task: asyncio.Task | None = None  # type: ignore[name-defined]

    async def start(self) -> None:
        if self.interval_seconds <= 0:
            logger.info("PT AI scan status poller disabled (PTAI_SCAN_STATUS_POLL_INTERVAL_SECONDS<=0)")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        logger.info("PT AI scan status poller started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("PT AI scan status poller stopped")

    async def _run(self) -> None:
        while True:
            try:
                with SessionLocal() as db:
                    n = await run_ptai_scan_status_cycle(db)
                    if n:
                        logger.info("Sent %s PT AI scan status alert(s)", n)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PT AI scan status cycle failed")
            await asyncio.sleep(self.interval_seconds)
