"""Alert when the PT AI engine queue grows too large."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import AppState, SessionLocal
from ..ptai_client import PtaiClient
from .notifications import notify_long_running_job
from ..integration_settings import runtime_setting

logger = logging.getLogger(__name__)

STATE_KEY = "ptai_queue_last_alert_at"
DEFAULT_QUEUE_STAGES = "Enqueued,Queued"


@dataclass
class PtaiQueuedProject:
    project_id: str
    project_name: str
    branch_id: str
    branch_name: str
    stage: str
    progress: int | None = None


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _channels_configured() -> bool:
    return bool(runtime_setting("MATTERMOST_WEBHOOK_URL", "").strip()) or bool(
        (os.getenv("ALERT_EMAIL_TO") or "").strip()
        and (os.getenv("ALERT_SMTP_HOST") or "").strip()
    )


def _queue_stages() -> set[str]:
    raw = os.getenv("PTAI_QUEUE_ALERT_STAGES", DEFAULT_QUEUE_STAGES)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get_last_alert_at(db: Session) -> datetime | None:
    row = db.query(AppState).filter(AppState.key == STATE_KEY).first()
    return _parse_time(row.value if row else None)


def _set_last_alert_at(db: Session, when: datetime | None) -> None:
    row = db.query(AppState).filter(AppState.key == STATE_KEY).first()
    value = when.isoformat() if when else ""
    if row:
        row.value = value
    else:
        row = AppState(key=STATE_KEY, value=value)
    db.add(row)


def _project_label(project: dict[str, Any]) -> str:
    return str(
        project.get("name")
        or project.get("displayName")
        or project.get("path")
        or project.get("slug")
        or project.get("id")
        or "unknown"
    )


def _project_id(project: dict[str, Any]) -> str | None:
    value = project.get("id") or project.get("projectId")
    return str(value) if value else None


def _branch_label(branch: dict[str, Any]) -> str:
    return str(branch.get("name") or branch.get("displayName") or branch.get("id") or "unknown")


def _branch_id(branch: dict[str, Any]) -> str | None:
    value = branch.get("id") or branch.get("branchId")
    return str(value) if value else None


def collect_ptai_queue(
    client: PtaiClient,
    *,
    queue_stages: set[str],
    project_limit: int,
    branch_limit: int,
) -> list[PtaiQueuedProject]:
    """Return projects whose latest branch scan result is in a queue stage."""
    queued: list[PtaiQueuedProject] = []
    projects = client.list_projects(limit=project_limit)[:project_limit]
    logger.info(
        "ptai_queue_collect_start projects=%s project_limit=%s branch_limit=%s queue_stages=%s",
        len(projects),
        project_limit,
        branch_limit,
        sorted(queue_stages),
    )
    inspected_branches = 0
    for project in projects:
        pid = _project_id(project)
        if not pid:
            logger.info("ptai_queue_project_skip reason=missing_project_id project=%s", _project_label(project))
            continue
        branches = client.list_branches(pid)[:branch_limit]
        logger.debug(
            "ptai_queue_project_branches project_id=%s project=%s branches=%s",
            pid,
            _project_label(project),
            len(branches),
        )
        for branch in branches:
            bid = _branch_id(branch)
            if not bid:
                logger.info(
                    "ptai_queue_branch_skip reason=missing_branch_id project_id=%s project=%s branch=%s",
                    pid,
                    _project_label(project),
                    _branch_label(branch),
                )
                continue
            inspected_branches += 1
            result = client.get_last_scan_result(bid)
            progress = (result or {}).get("progress") if result else {}
            stage = str((progress or {}).get("stage") or "").strip()
            if stage.lower() not in queue_stages:
                continue
            queued.append(
                PtaiQueuedProject(
                    project_id=pid,
                    project_name=_project_label(project),
                    branch_id=bid,
                    branch_name=_branch_label(branch),
                    stage=stage,
                    progress=(progress or {}).get("value"),
                )
            )
            # Alert by projects, not by branches: one queued branch is enough.
            break
    logger.info(
        "ptai_queue_collect_finish projects=%s branches=%s queued_projects=%s",
        len(projects),
        inspected_branches,
        len(queued),
    )
    return queued


def build_queue_alert_text(queued: list[PtaiQueuedProject], threshold: int) -> str:
    lines = [
        f"**PT AI queue is high** — {len(queued)} queued project(s), threshold {threshold}",
        "",
        "Top queued projects:",
    ]
    for item in queued[:20]:
        progress = f", {item.progress}%" if item.progress is not None else ""
        lines.append(
            f"- `{item.project_name}` / `{item.branch_name}` — stage `{item.stage}`{progress}"
        )
    if len(queued) > 20:
        lines.append(f"- ...and {len(queued) - 20} more")
    return "\n".join(lines)


async def run_ptai_queue_alert_cycle(db: Session) -> int:
    if not _bool_env("PTAI_QUEUE_ALERT_ENABLED", True):
        logger.info("ptai_queue_cycle_skipped reason=disabled")
        return 0
    if not _channels_configured():
        logger.warning(
            "PT AI queue alert enabled but no channel configured "
            "(connect Mattermost in Integrations and/or configure email alerts)"
        )
        return 0

    client = PtaiClient()
    if not client.enabled():
        logger.warning("PT AI queue check skipped: connector not configured in Integrations")
        return 0

    threshold = int(os.getenv("PTAI_QUEUE_ALERT_THRESHOLD", "10"))
    resend_minutes = int(os.getenv("PTAI_QUEUE_ALERT_RESEND_MINUTES", "60"))
    project_limit = int(os.getenv("PTAI_QUEUE_PROJECT_LIMIT", "500"))
    branch_limit = int(os.getenv("PTAI_QUEUE_BRANCH_LIMIT", "20"))
    now = datetime.now(timezone.utc)
    logger.info(
        "ptai_queue_cycle_start threshold=%s resend_minutes=%s project_limit=%s branch_limit=%s",
        threshold,
        resend_minutes,
        project_limit,
        branch_limit,
    )

    queued = await asyncio.to_thread(
        collect_ptai_queue,
        client,
        queue_stages=_queue_stages(),
        project_limit=project_limit,
        branch_limit=branch_limit,
    )
    if len(queued) < threshold:
        _set_last_alert_at(db, None)
        db.commit()
        logger.info("ptai_queue_cycle_finish queued=%s threshold=%s alert=false reason=below_threshold", len(queued), threshold)
        return 0

    last_alert_at = _get_last_alert_at(db)
    if last_alert_at:
        if last_alert_at.tzinfo is None:
            last_alert_at = last_alert_at.replace(tzinfo=timezone.utc)
        if resend_minutes <= 0 or now - last_alert_at < timedelta(minutes=resend_minutes):
            logger.info(
                "ptai_queue_alert_suppressed queued=%s threshold=%s last_alert_at=%s resend_minutes=%s",
                len(queued),
                threshold,
                last_alert_at.isoformat(),
                resend_minutes,
            )
            return 0

    ok = await notify_long_running_job(
        text=build_queue_alert_text(queued, threshold),
        subject=f"[ScanOps] PT AI queue high: {len(queued)} queued projects",
    )
    if not ok:
        logger.warning("ptai_queue_alert_delivery_failed queued=%s threshold=%s", len(queued), threshold)
        return 0
    _set_last_alert_at(db, now)
    db.commit()
    logger.info("ptai_queue_alert_sent queued=%s threshold=%s", len(queued), threshold)
    return 1


class PtaiQueueAlertPoller:
    """Background loop: alert when PT AI has too many queued projects."""

    def __init__(self) -> None:
        self.interval_seconds = int(os.getenv("PTAI_QUEUE_ALERT_INTERVAL_SECONDS", "300"))
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.interval_seconds <= 0:
            logger.info("PT AI queue alert poller disabled (PTAI_QUEUE_ALERT_INTERVAL_SECONDS<=0)")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        logger.info("PT AI queue alert poller started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("PT AI queue alert poller stopped")

    async def _run(self) -> None:
        while True:
            try:
                with SessionLocal() as db:
                    n = await run_ptai_queue_alert_cycle(db)
                    if n:
                        logger.info("Sent PT AI queue alert")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PT AI queue alert cycle failed")
            await asyncio.sleep(self.interval_seconds)
