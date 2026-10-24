"""Worker liveness: DB heartbeat rows + a local file for docker healthchecks.

Each long-running worker loop calls ``beat("<worker-name>")`` once per
iteration. That upserts a row in ``worker_heartbeats`` (visible to
``/api/health`` across containers) and touches a local file (visible to the
docker healthcheck inside the same container).

Failures here must never kill a worker loop — everything is best-effort.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path(os.getenv("HEARTBEAT_FILE", "/tmp/secpipe-heartbeat"))

_UPSERT = text(
    """
    INSERT INTO worker_heartbeats (name, last_beat_at)
    VALUES (:name, :now)
    ON CONFLICT (name) DO UPDATE SET last_beat_at = EXCLUDED.last_beat_at
    """
)


def beat(name: str) -> None:
    now = datetime.now(timezone.utc)
    try:
        HEARTBEAT_FILE.touch()
    except OSError:
        pass
    try:
        from .models import engine

        with engine.begin() as conn:
            conn.execute(_UPSERT, {"name": name, "now": now})
    except Exception:
        # DB hiccups must not crash the worker; the stale heartbeat itself
        # is the signal that something is wrong.
        logger.warning("Heartbeat for %s failed", name, exc_info=True)
