import asyncio
import logging
import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

import httpx

from .models import SessionLocal
from .models.pipeline import ActivePipeline
from .database import DatabaseService
from .ptai_capture import is_ptai_job, parse_scan_ids
from .integration_settings import runtime_setting

logger = logging.getLogger(__name__)


class PipelinePoller:
    def __init__(self):
        self.enabled = os.getenv("PIPELINE_POLLER_ENABLED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.poll_interval = int(os.getenv("PIPELINE_POLL_INTERVAL_SECONDS", "300"))
        self.batch_size = int(os.getenv("PIPELINE_POLL_BATCH_SIZE", "10"))
        self.max_age_seconds = int(os.getenv("PIPELINE_MAX_AGE_SECONDS", str(60 * 60 * 24)))
        self.gitlab_api_url = ""
        self.gitlab_pat = ""
        self.instance_map = {}
        self._refresh_integration_config()
        self._task: asyncio.Task | None = None

    def _refresh_integration_config(self) -> None:
        self.gitlab_api_url = runtime_setting("GITLAB_API_URL", "https://gitlab.com/api/v4").rstrip("/")
        self.gitlab_pat = runtime_setting("GITLAB_PAT", "")
        self.instance_map = self._load_instance_map(runtime_setting("GITLAB_INSTANCES", ""))

    def is_enabled(self) -> bool:
        self._refresh_integration_config()
        return self.enabled and bool(self.gitlab_pat or self.instance_map)

    async def start(self):
        if not self.enabled:
            logger.warning(
                "pipeline_poller_disabled reason=feature_disabled enabled=%s instances=%s",
                self.enabled,
                len(self.instance_map),
            )
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            self._refresh_integration_config()
            logger.info(
                "pipeline_poller_start interval=%s batch_size=%s max_age_seconds=%s instances=%s credentials=%s",
                self.poll_interval,
                self.batch_size,
                self.max_age_seconds,
                len(self.instance_map),
                bool(self.gitlab_pat or self.instance_map),
            )

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Pipeline poller stopped")

    async def _run(self):
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pipeline poller iteration failed")
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self):
        self._refresh_integration_config()
        if not (self.gitlab_pat or self.instance_map):
            logger.debug("pipeline_poller_waiting reason=missing_gitlab_credentials")
            return
        started = time.monotonic()
        with SessionLocal() as db:
            service = DatabaseService(db)
            stale_before = datetime.now(timezone.utc) - timedelta(seconds=self.max_age_seconds)
            service.mark_pipelines_stale(stale_before)
            pipelines = service.fetch_active_pipelines(limit=self.batch_size)
            if not pipelines:
                logger.debug("pipeline_poller_empty batch_size=%s", self.batch_size)
                return
            logger.info("pipeline_poller_selected count=%s batch_size=%s", len(pipelines), self.batch_size)

            for pipeline in pipelines:
                api_url, token = self._resolve_instance_config(pipeline)
                if not api_url or not token:
                    logger.warning(
                        "pipeline_poll_skipped reason=missing_gitlab_token active_pipeline_id=%s project_id=%s pipeline_id=%s repo=%s",
                        pipeline.id,
                        pipeline.project_id,
                        pipeline.pipeline_id,
                        getattr(getattr(pipeline, "repository", None), "path", None),
                    )
                    service.update_pipeline_poll_state(pipeline, completed=False, error="GitLab token missing")
                    continue
                async with httpx.AsyncClient(
                    base_url=api_url,
                    headers={"PRIVATE-TOKEN": token},
                    timeout=30,
                ) as client:
                    await self._poll_pipeline(client, service, pipeline)
            logger.info(
                "pipeline_poller_finish count=%s duration_ms=%s",
                len(pipelines),
                int((time.monotonic() - started) * 1000),
            )

    async def _poll_pipeline(self, client: httpx.AsyncClient, service: DatabaseService, pipeline: ActivePipeline):
        endpoint = (
            f"/projects/{pipeline.project_id}/pipelines/{pipeline.pipeline_id}"
            "/jobs?per_page=100&include_retried=false"
        )
        logger.info(
            "pipeline_poll_start active_pipeline_id=%s project_id=%s pipeline_id=%s tracked_jobs=%s",
            pipeline.id,
            pipeline.project_id,
            pipeline.pipeline_id,
            pipeline.tracked_jobs or "all",
        )
        try:
            response = await client.get(endpoint)
            response.raise_for_status()
            jobs = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                service.update_pipeline_poll_state(pipeline, completed=True, error="Pipeline not found (treated as complete)")
            else:
                service.update_pipeline_poll_state(pipeline, completed=False, error=str(exc))
            logger.warning(
                "pipeline_poll_http_error active_pipeline_id=%s project_id=%s pipeline_id=%s http_code=%s error=%s",
                pipeline.id,
                pipeline.project_id,
                pipeline.pipeline_id,
                exc.response.status_code,
                exc,
            )
            return
        except Exception as exc:
            service.update_pipeline_poll_state(pipeline, completed=False, error=str(exc))
            logger.warning(
                "pipeline_poll_failed active_pipeline_id=%s project_id=%s pipeline_id=%s error=%s",
                pipeline.id,
                pipeline.project_id,
                pipeline.pipeline_id,
                exc,
            )
            return

        logger.info(
            "pipeline_poll_jobs_loaded active_pipeline_id=%s project_id=%s pipeline_id=%s jobs=%s",
            pipeline.id,
            pipeline.project_id,
            pipeline.pipeline_id,
            len(jobs) if isinstance(jobs, list) else "invalid",
        )

        # Capture async PT AI scans from the FULL job list (not the tracked
        # filter): ptai_scan may not be in tracked_jobs, and once captured we
        # follow the real scan inside ASOC, not the GitLab job.
        await self._capture_ptai_scans(client, service, pipeline, jobs)

        tracked_jobs = self._filter_jobs(jobs, pipeline.tracked_jobs)
        if not tracked_jobs:
            logger.info(
                "pipeline_poll_no_tracked_jobs active_pipeline_id=%s project_id=%s pipeline_id=%s tracked_jobs=%s available_jobs=%s",
                pipeline.id,
                pipeline.project_id,
                pipeline.pipeline_id,
                pipeline.tracked_jobs,
                [job.get("name") for job in jobs[:30]],
            )

        final_statuses = {"success", "failed", "canceled", "cancelled", "skipped", "manual"}
        in_progress_statuses = {"pending", "running", "created"}

        for job_payload in tracked_jobs:
            try:
                service.record_pipeline_job(pipeline, job_payload)
            except Exception as exc:
                logger.warning(
                    "Failed to persist job %s from pipeline %s/%s: %s",
                    job_payload.get("id"),
                    pipeline.project_id,
                    pipeline.pipeline_id,
                    exc,
                )

        completed = bool(tracked_jobs) and all(
            (job_payload.get("status") or "").lower() in final_statuses for job_payload in tracked_jobs
        )

        if not completed:
            # If any job is still pending/running, keep polling
            for job_payload in tracked_jobs:
                if (job_payload.get("status") or "").lower() in in_progress_statuses:
                    completed = False
                    break

        service.update_pipeline_poll_state(
            pipeline,
            completed=completed,
            error=None,
            observed_job_statuses=(
                (job_payload.get("status") or "")
                for job_payload in tracked_jobs
            ),
        )
        logger.info(
            "pipeline_poll_finish active_pipeline_id=%s project_id=%s pipeline_id=%s tracked=%s completed=%s",
            pipeline.id,
            pipeline.project_id,
            pipeline.pipeline_id,
            len(tracked_jobs),
            completed,
        )

    async def _capture_ptai_scans(
        self,
        client: httpx.AsyncClient,
        service: DatabaseService,
        pipeline: ActivePipeline,
        jobs: List[dict],
    ) -> None:
        """Read finished ptai_scan job traces and persist the ASOC ids once."""
        # Only jobs that actually ran can carry a trace with ids.
        ran_statuses = {"success", "failed"}
        ptai_jobs = [job for job in jobs if is_ptai_job(job.get("name"))]
        if ptai_jobs:
            logger.info(
                "ptai_capture_candidates active_pipeline_id=%s project_id=%s pipeline_id=%s jobs=%s",
                pipeline.id,
                pipeline.project_id,
                pipeline.pipeline_id,
                [
                    {"id": job.get("id"), "name": job.get("name"), "status": job.get("status")}
                    for job in ptai_jobs[:20]
                ],
            )
        for job in jobs:
            if not is_ptai_job(job.get("name")):
                continue
            if (job.get("status") or "").lower() not in ran_statuses:
                logger.info(
                    "ptai_capture_skip active_pipeline_id=%s job_id=%s reason=job_not_finished status=%s",
                    pipeline.id,
                    job.get("id"),
                    job.get("status"),
                )
                continue
            job_id = job.get("id")
            if job_id is None:
                logger.warning("ptai_capture_skip active_pipeline_id=%s reason=missing_job_id", pipeline.id)
                continue
            if service.ptai_scan_exists(job_id):
                logger.info("ptai_capture_skip active_pipeline_id=%s job_id=%s reason=already_recorded", pipeline.id, job_id)
                continue
            trace = await self._fetch_job_trace(client, pipeline.project_id, job_id)
            parsed = parse_scan_ids(trace)
            logger.info(
                "ptai_capture_trace_parsed active_pipeline_id=%s job_id=%s has_trace=%s parsed_keys=%s",
                pipeline.id,
                job_id,
                bool(trace),
                sorted(parsed.keys()) if parsed else [],
            )
            try:
                scan = service.record_ptai_scan(pipeline, job, parsed)
            except Exception as exc:
                logger.warning(
                    "Failed to record ptai scan for job %s (pipeline %s/%s): %s",
                    job_id, pipeline.project_id, pipeline.pipeline_id, exc,
                )
                continue
            if scan is not None:
                logger.info(
                    "ptai_capture_recorded active_pipeline_id=%s job_id=%s scan_id=%s asoc_pipeline_id=%s asoc_task_id=%s status=%s",
                    pipeline.id, job_id, scan.id, scan.asoc_pipeline_id, scan.asoc_task_id, scan.asoc_status,
                )

    @staticmethod
    async def _fetch_job_trace(
        client: httpx.AsyncClient, project_id: int, job_id: int
    ) -> str | None:
        try:
            response = await client.get(f"/projects/{project_id}/jobs/{job_id}/trace")
            response.raise_for_status()
            return response.text
        except Exception as exc:
            logger.warning("Failed to fetch trace for job %s: %s", job_id, exc)
            return None

    @staticmethod
    def _filter_jobs(jobs: List[dict], tracked: List[str] | None) -> List[dict]:
        if not tracked:
            return jobs
        tracked_lower = {item.lower() for item in tracked}
        return [job for job in jobs if (job.get("name") or "").lower() in tracked_lower]

    def _load_instance_map(self, raw: str) -> Dict[str, Dict[str, str]]:
        raw = (raw or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse GITLAB_INSTANCES, expected JSON")
            return {}
        mapping: Dict[str, Dict[str, str]] = {}
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                host = entry.get("host")
                api_url = entry.get("api_url")
                token = entry.get("token")
                if host and api_url and token:
                    mapping[host] = {"api_url": api_url.rstrip("/"), "token": token}
        elif isinstance(data, dict):
            for host, cfg in data.items():
                if isinstance(cfg, dict):
                    api_url = cfg.get("api_url")
                    token = cfg.get("token")
                    if host and api_url and token:
                        mapping[host] = {"api_url": api_url.rstrip("/"), "token": token}
        return mapping

    def _resolve_instance_config(self, pipeline: ActivePipeline) -> (Optional[str], Optional[str]):
        host = None
        if getattr(pipeline, "repository", None) and getattr(pipeline.repository, "product", None):
            base = pipeline.repository.product.gitlab_base_url
            if base:
                parsed = urlparse(base)
                host = parsed.netloc or base
        if host and host in self.instance_map:
            cfg = self.instance_map[host]
            return cfg["api_url"], cfg["token"]
        return (self.gitlab_api_url, self.gitlab_pat)
