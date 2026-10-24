"""Pipeline worker entrypoint: `python -m app.workers.pipeline`.

Runs the GitLab pipeline poller, the long-running-job alert poller, the
periodic ASOC product-card sync, daily retention cleanup and a liveness
heartbeat. No uvicorn, no enable-flag
gymnastics: this process does exactly this and nothing else. The web app
(`app.main`) no longer starts these pollers at all, so the old
PIPELINE_POLLER_ENABLED juggling between containers is gone.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from ..alerting.secpipe_job_duration import JobDurationAlertPoller
from ..alerting.ptai_scan_status import PtaiScanStatusPoller
from ..alerting.ptai_queue import PtaiQueueAlertPoller
from ..asoc_enrichment import run_enrichment_once
from ..asoc_products import asoc_sync_interval_minutes, run_scheduled_sync
from ..asoc_description_safety import sanitize_existing_asoc_descriptions
from ..audit_digest import maybe_send_digest
from ..heartbeat import beat
from ..models import Base, SessionLocal, engine
from ..migrations_runner import PYTHON_MIGRATIONS, run_migrations, schema_bootstrap_lock
from ..pipeline_tracker import PipelinePoller
from ..retention import cleanup_once

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60
RETENTION_INTERVAL = timedelta(hours=24)


def _sync_asoc_products() -> None:
    db = SessionLocal()
    try:
        outcome = run_scheduled_sync(db)
        if outcome:
            logger.info("asoc_product_auto_sync status=%s", outcome["status"])
    finally:
        db.close()


# A healthy ASOC sync takes seconds; past this the worker stops reporting
# itself alive so a hung upstream call shows up as a stale heartbeat.
SYNC_HEARTBEAT_LIMIT = timedelta(minutes=15)


async def _run_while_beating(func, limit: timedelta = SYNC_HEARTBEAT_LIMIT) -> None:
    """Run blocking work in a thread without letting the heartbeat go stale."""
    task = asyncio.ensure_future(asyncio.to_thread(func))
    deadline = datetime.now(timezone.utc) + limit
    reported = False
    while True:
        done, _pending = await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL_SECONDS)
        if done:
            task.result()
            return
        if datetime.now(timezone.utc) < deadline:
            beat("pipeline_worker")
        elif not reported:
            reported = True
            logger.error("Blocking worker step exceeded %s; heartbeat paused until it returns", limit)


async def main() -> None:
    # The worker can start before the web container.  Ensure it never queries
    # a model column that was added by a pending SQL migration.
    with schema_bootstrap_lock(engine):
        Base.metadata.create_all(bind=engine)
        PYTHON_MIGRATIONS.setdefault(
            "20260925_sanitize_asoc_descriptions",
            sanitize_existing_asoc_descriptions,
        )
        run_migrations(engine)

    poller = PipelinePoller()
    alerts = JobDurationAlertPoller()
    ptai_status = PtaiScanStatusPoller()
    ptai_queue = PtaiQueueAlertPoller()
    await poller.start()
    await alerts.start()
    await ptai_status.start()
    await ptai_queue.start()
    logger.info("Pipeline worker started")

    last_daily: datetime | None = None
    last_asoc_sync: datetime | None = None
    try:
        while True:
            beat("pipeline_worker")
            now = datetime.now(timezone.utc)

            # Audit digest keeps its own last-sent state in the DB, so calling
            # it every loop tick is cheap; it decides internally when to send.
            try:
                await maybe_send_digest()
            except Exception:
                logger.exception("Audit digest attempt failed")

            asoc_interval = asoc_sync_interval_minutes()
            if asoc_interval and (
                last_asoc_sync is None
                or now - last_asoc_sync >= timedelta(minutes=asoc_interval)
            ):
                try:
                    await _run_while_beating(_sync_asoc_products)
                except Exception:
                    logger.exception("ASOC product sync attempt failed")
                last_asoc_sync = now

            if last_daily is None or now - last_daily >= RETENTION_INTERVAL:
                # Blocking DB deletes / feed downloads are fine here: daily.
                await asyncio.to_thread(cleanup_once, engine)
                try:
                    await asyncio.to_thread(run_enrichment_once)
                except Exception:
                    logger.exception("ASOC enrichment pass failed")
                last_daily = now
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
    finally:
        await poller.stop()
        await alerts.stop()
        await ptai_status.stop()
        await ptai_queue.stop()


if __name__ == "__main__":
    asyncio.run(main())
