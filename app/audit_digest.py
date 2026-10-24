"""Weekly Mattermost digest of repositories without working security pipelines.

Turns the audit page from "a dashboard someone must remember to open" into a
push notification. Sends a grouped summary of problem repositories (security
pipeline missing, no pipelines at all, inaccessible) to the team channel.

State (when the digest was last sent) lives in the app_state table so worker
restarts do not re-spam the channel.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from .alerting.notifications import send_mattermost_incoming_webhook
from .audit_service import AuditService
from .integration_settings import runtime_setting
from .models import AppState, SessionLocal
from .models.audit import AuditCoverageStatus
from .target_navigation import COVERAGE_PATH

logger = logging.getLogger(__name__)

STATE_KEY = "audit_digest_last_sent"

_STATUS_TITLES = {
    AuditCoverageStatus.PIPELINE_NO_SEC: "Security job is missing",
    AuditCoverageStatus.NO_PIPELINES: "Pipelines are not running",
    AuditCoverageStatus.INACCESSIBLE: "Repository is unavailable to the audit",
}

MAX_REPOS_PER_GROUP = 30


def _enabled() -> bool:
    return os.getenv("AUDIT_DIGEST_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _interval() -> timedelta:
    try:
        days = int(os.getenv("AUDIT_DIGEST_INTERVAL_DAYS", "7"))
    except ValueError:
        days = 7
    return timedelta(days=max(days, 1))


def _get_last_sent(db) -> datetime | None:
    row = db.query(AppState).filter(AppState.key == STATE_KEY).first()
    if not row or not row.value:
        return None
    try:
        value = datetime.fromisoformat(row.value)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _set_last_sent(db, when: datetime) -> None:
    row = db.query(AppState).filter(AppState.key == STATE_KEY).first()
    if row:
        row.value = when.isoformat()
    else:
        db.add(AppState(key=STATE_KEY, value=when.isoformat()))
    db.commit()


def build_digest_text(rows: list[dict]) -> str | None:
    """Group problem rows by status into a Mattermost-friendly message."""
    if not rows:
        return None
    groups: dict[AuditCoverageStatus, list[dict]] = {}
    for row in rows:
        status = row["snapshot"].status
        groups.setdefault(status, []).append(row)

    base_url = os.getenv("SECPIPE_PUBLIC_URL", "").rstrip("/")
    lines = [f"#### Security pipeline coverage audit: {len(rows)} repositories requiring attention"]
    for status, title in _STATUS_TITLES.items():
        bucket = groups.get(status) or []
        if not bucket:
            continue
        lines.append(f"\n**{title} — {len(bucket)}:**")
        for row in bucket[:MAX_REPOS_PER_GROUP]:
            repo = row["repository"]
            path = repo.path_with_namespace or "?"
            if repo.web_url:
                lines.append(f"- [{path}]({repo.web_url})")
            else:
                lines.append(f"- {path}")
        if len(bucket) > MAX_REPOS_PER_GROUP:
            lines.append(f"- … and {len(bucket) - MAX_REPOS_PER_GROUP} more")
    if base_url:
        lines.append(f"\nFull list: {base_url}{COVERAGE_PATH}")
    return "\n".join(lines)


async def maybe_send_digest() -> bool:
    """Send the digest if enabled and the interval has elapsed. Returns True when sent."""
    if not _enabled():
        return False
    webhook = runtime_setting("MATTERMOST_WEBHOOK_URL", "").strip()
    if not webhook:
        logger.warning("Audit digest enabled but Mattermost is not connected in Integrations")
        return False

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        last = _get_last_sent(db)
        if last and now - last < _interval():
            return False
        rows = AuditService(db).get_latest_rows(only_problems=True)
        text = build_digest_text(rows)
        if text is None:
            # No problems — still record the attempt so we don't query daily.
            _set_last_sent(db, now)
            logger.info("Audit digest: no problem repositories, nothing to send")
            return False

    async with httpx.AsyncClient() as client:
        await send_mattermost_incoming_webhook(client, webhook, text=text, username="ScanOps Audit")

    with SessionLocal() as db:
        _set_last_sent(db, now)
    logger.info("Audit digest sent (%s problem repos)", len(rows))
    return True
