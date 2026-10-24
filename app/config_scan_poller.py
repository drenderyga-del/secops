from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import joinedload

from .models import SessionLocal, Product, ActivePipeline, Repository
from .config_scan_service import ConfigScanService
from .config_scan_reports import write_report_files
from .config_scanner import GitLabScanner
from .gitlab_url import gitlab_web_origin, looks_like_gitlab_web_url
from .integration_settings import runtime_setting

logger = logging.getLogger(__name__)

_DEFAULT_FULL_PRODUCT_INTERVAL = 30 * 24 * 3600


class ConfigScanPoller:
    def __init__(self) -> None:
        self.enabled = os.getenv("CONFIG_SCAN_ENABLED", "true").lower() in ("1", "true", "yes")
        self.full_product_interval_seconds = int(
            os.getenv("CONFIG_SCAN_FULL_PRODUCT_INTERVAL_SECONDS", str(_DEFAULT_FULL_PRODUCT_INTERVAL))
        )
        self.full_product_scan_on_start = os.getenv(
            "CONFIG_SCAN_FULL_PRODUCT_ON_START", "false"
        ).lower() in ("1", "true", "yes")
        # Periodic full-product scan loop (monthly by default). Set false on extra app replicas
        # so only one process runs scheduled_full scans; pipeline/manual/URL triggers still work.
        self.run_background_scheduler = os.getenv(
            "CONFIG_SCAN_RUN_BACKGROUND_SCHEDULER", "true"
        ).lower() in ("1", "true", "yes")
        self.concurrency = int(os.getenv("CONFIG_SCAN_CONCURRENCY", "12"))
        self.request_timeout = int(os.getenv("CONFIG_SCAN_REQUEST_TIMEOUT", "30"))
        self.max_include_depth = int(os.getenv("CONFIG_SCAN_MAX_INCLUDE_DEPTH", "10"))
        self.verify_ssl = os.getenv("CONFIG_SCAN_VERIFY_SSL", "true").lower() in ("1", "true", "yes")

        self.gitlab_pat = ""
        self.instance_map = {}
        self._refresh_integration_config()
        self._task: asyncio.Task | None = None

    def _refresh_integration_config(self) -> None:
        self.gitlab_pat = runtime_setting("GITLAB_PAT", "")
        self.instance_map = self._load_instance_map(runtime_setting("GITLAB_INSTANCES", ""))

    def is_enabled(self) -> bool:
        self._refresh_integration_config()
        return self.enabled and bool(self.gitlab_pat or self.instance_map)

    async def start(self) -> None:
        if not self.enabled:
            logger.warning(
                "config_scan_poller_disabled reason=feature_disabled enabled=%s instances=%s has_default_token=%s",
                self.enabled,
                len(self.instance_map),
                bool(self.gitlab_pat),
            )
            return
        if self.run_background_scheduler:
            if self._task is None:
                self._task = asyncio.create_task(self._run())
                logger.info(
                    "config_scan_scheduler_start interval=%s on_start=%s concurrency=%s timeout=%s verify_ssl=%s instances=%s",
                    self.full_product_interval_seconds,
                    self.full_product_scan_on_start,
                    self.concurrency,
                    self.request_timeout,
                    self.verify_ssl,
                    len(self.instance_map),
                )
        else:
            logger.info(
                "config_scan_scheduler_disabled reason=background_scheduler_off manual_triggers_enabled=true"
            )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Config scan poller stopped")

    async def _run(self) -> None:
        if self.full_product_scan_on_start:
            try:
                await self._full_product_scan_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Config scan initial full-product pass failed")
        while True:
            try:
                await asyncio.sleep(self.full_product_interval_seconds)
                await self._full_product_scan_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Config scan full-product iteration failed")

    async def _full_product_scan_all(self) -> None:
        self._refresh_integration_config()
        if not (self.gitlab_pat or self.instance_map):
            logger.info("config_scan_full_product_waiting reason=missing_gitlab_credentials")
            return
        with SessionLocal() as db:
            products = db.query(Product).order_by(Product.id.asc()).all()
            if not products:
                logger.debug("config_scan_full_product_empty")
                return
            logger.info("config_scan_full_product_selected products=%s", len(products))

            for product in products:
                try:
                    await self._scan_product(product)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Config scan failed for product %s (id=%s)",
                        product.name,
                        product.id,
                    )

    async def _scan_product(self, product: Product) -> None:
        gitlab_url, token = self._resolve_instance_config(product)
        if not gitlab_url or not token:
            logger.warning(
                "config_scan_product_skipped reason=missing_gitlab_token product_id=%s product=%s",
                product.id,
                product.name,
            )
            return

        group_path = self._resolve_poller_scope(product)
        if not group_path:
            logger.debug(
                "config_scan_product_skipped reason=empty_scope product_id=%s product=%s",
                product.id,
                product.name,
            )
            return

        with SessionLocal() as db:
            service = ConfigScanService(db)
            run = service.create_run(
                product.id, trigger="scheduled_full", scope_path=group_path
            )
            run_id = run.id
            product_id = product.id
            product_name = product.name

        logger.info(
            "config_scan_queued trigger=scheduled_full product_id=%s product=%s scope=%s run_id=%s include_subgroups=true",
            product_id,
            product_name,
            group_path,
            run_id,
        )

        await self._execute_scan_run(
            product_id=product_id,
            run_id=run_id,
            gitlab_url=gitlab_url,
            token=token,
            scope_path=group_path,
            include_subgroups=True,
            log_context=f"full_product name={product_name}",
        )

    def schedule_pipeline_repo_scan(self, active_pipeline_id: int) -> None:
        """Queue config scan for the repository tied to a newly registered pipeline."""
        if not self.is_enabled():
            return
        asyncio.create_task(self._execute_pipeline_repo_scan(active_pipeline_id))

    async def _execute_pipeline_repo_scan(self, active_pipeline_id: int) -> None:
        with SessionLocal() as db:
            pl = (
                db.query(ActivePipeline)
                .options(
                    joinedload(ActivePipeline.repository),
                    joinedload(ActivePipeline.product),
                )
                .filter(ActivePipeline.id == active_pipeline_id)
                .first()
            )
            if not pl or not pl.repository:
                logger.warning(
                    "config_scan_pipeline_skipped active_pipeline_id=%s reason=missing_repository",
                    active_pipeline_id,
                )
                return
            scope = (pl.repository.path or "").strip().strip("/")
            product = pl.product
            if not product:
                logger.warning(
                    "config_scan_pipeline_skipped active_pipeline_id=%s reason=missing_product",
                    active_pipeline_id,
                )
                return
            product_id = pl.product_id
            product_name = product.name
            gitlab_url, token = self._resolve_instance_config(product)
            if not gitlab_url or not token:
                logger.warning(
                    "config_scan_pipeline_skipped active_pipeline_id=%s product_id=%s product=%s reason=missing_gitlab_token",
                    active_pipeline_id,
                    product_id,
                    product_name,
                )
                return
            if not scope:
                logger.warning(
                    "config_scan_pipeline_skipped active_pipeline_id=%s product_id=%s product=%s reason=empty_repository_path",
                    active_pipeline_id,
                    product_id,
                    product_name,
                )
                return
            service = ConfigScanService(db)
            run = service.create_run(product_id, trigger="pipeline", scope_path=scope)
            run_id = run.id

        logger.info(
            "config_scan_queued trigger=pipeline active_pipeline_id=%s product_id=%s product=%s scope=%s run_id=%s include_subgroups=false",
            active_pipeline_id,
            product_id,
            product_name,
            scope,
            run_id,
        )

        await self._execute_scan_run(
            product_id=product_id,
            run_id=run_id,
            gitlab_url=gitlab_url,
            token=token,
            scope_path=scope,
            include_subgroups=False,
            log_context=f"pipeline_repo product={product_name} path={scope}",
        )

    async def _execute_scan_run(
        self,
        *,
        product_id: int,
        run_id: int,
        gitlab_url: str,
        token: str,
        scope_path: str,
        include_subgroups: bool,
        log_context: str,
    ) -> None:
        try:
            started = time.monotonic()
            with SessionLocal() as db:
                service = ConfigScanService(db)
                service.mark_run_started(run_id)
            logger.info(
                "config_scan_run_start run_id=%s product_id=%s scope=%s include_subgroups=%s context=%s verify_ssl=%s concurrency=%s",
                run_id,
                product_id,
                scope_path,
                include_subgroups,
                log_context,
                self.verify_ssl,
                self.concurrency,
            )

            async def _progress(total: int, scanned: int) -> None:
                if scanned % 10 == 0 or scanned == total:
                    with SessionLocal() as db:
                        service = ConfigScanService(db)
                        service.update_run_progress(run_id, total, scanned)
                    logger.info(
                        "config_scan_progress run_id=%s total=%s scanned=%s context=%s",
                        run_id,
                        total,
                        scanned,
                        log_context,
                    )

            scanner = GitLabScanner(
                gitlab_url=gitlab_web_origin(gitlab_url),
                token=token,
                group_path=scope_path,
                verify_ssl=self.verify_ssl,
                include_subgroups=include_subgroups,
                progress_callback=_progress,
                project_scan_concurrency=self.concurrency,
                request_timeout=self.request_timeout,
                max_include_depth=self.max_include_depth,
            )
            report = await scanner.scan()

            summary = dict(report.get("summary") or {})
            projects = report.get("projects") or []
            nproj = len(projects)
            if nproj:
                if not isinstance(summary.get("projects_total"), int):
                    summary["projects_total"] = nproj
                if not isinstance(summary.get("projects_scanned"), int):
                    summary["projects_scanned"] = nproj
            report = {**report, "summary": summary}

            json_path, html_path, _, _ = write_report_files(report, run_id)

            with SessionLocal() as db:
                service = ConfigScanService(db)
                findings_count = service.save_findings(run_id, product_id, projects)
                service.complete_run(
                    run_id, summary, findings_count,
                    report_json_path=json_path,
                    report_html_path=html_path,
                )

            logger.info(
                "config_scan_run_finish run_id=%s product_id=%s context=%s projects=%s findings=%s duration_ms=%s report_json=%s",
                run_id,
                product_id,
                log_context,
                summary.get("projects_scanned", 0),
                findings_count,
                int((time.monotonic() - started) * 1000),
                json_path,
            )

        except Exception as exc:
            with SessionLocal() as db:
                service = ConfigScanService(db)
                service.fail_run(run_id, str(exc))
            logger.exception(
                "config_scan_run_failed run_id=%s product_id=%s context=%s scope=%s error=%s",
                run_id,
                product_id,
                log_context,
                scope_path,
                exc,
            )

    async def trigger_manual_scan(self, product_ids: list[int]) -> list[int]:
        run_ids = []
        with SessionLocal() as db:
            products = (
                db.query(Product)
                .filter(Product.id.in_(product_ids))
                .all()
            )
            for product in products:
                # Manual scan must represent full product scope, not the scope of the
                # last run (which could be a one-repository manual_repo/targeted scan).
                base_scope = self._extract_scope_path(product) or self._find_last_scope(db, product.id)
                service = ConfigScanService(db)
                run = service.create_run(product.id, trigger="manual", scope_path=base_scope)
                logger.info(
                    "config_scan_queued trigger=manual product_id=%s product=%s scope=%s run_id=%s include_subgroups=true",
                    product.id,
                    product.name,
                    base_scope,
                    run.id,
                )
                run_ids.append(run.id)

        for product_id, run_id in zip(product_ids, run_ids):
            asyncio.create_task(self._execute_manual_scan(product_id, run_id))

        return run_ids

    async def trigger_repo_scan(self, product_id: int, repository_id: int) -> int:
        """Queue a config scan for one repository (full GitLab path_with_namespace)."""
        if not self.is_enabled():
            raise RuntimeError("Config scan is disabled or missing GitLab token")
        with SessionLocal() as db:
            repo = (
                db.query(Repository)
                .filter(Repository.id == repository_id)
                .first()
            )
            if not repo or repo.product_id != product_id:
                raise ValueError("Repository not found for this product")
            scope = (repo.path or "").strip().strip("/")
            if not scope:
                raise ValueError("Repository has no GitLab path")
            service = ConfigScanService(db)
            run = service.create_run(
                product_id, trigger="manual_repo", scope_path=scope
            )
            run_id = run.id

        logger.info(
            "Config scan queued (single repo): product_id=%s repository_id=%s path=%s run_id=%s",
            product_id,
            repository_id,
            scope,
            run_id,
        )

        asyncio.create_task(
            self._execute_repo_scan(product_id, run_id, scope),
        )
        return run_id

    async def _execute_repo_scan(
        self,
        product_id: int,
        run_id: int,
        scope_path: str,
    ) -> None:
        with SessionLocal() as db:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                service = ConfigScanService(db)
                service.fail_run(run_id, "Product not found")
                return
            gitlab_url, token = self._resolve_instance_config(product)

        if not gitlab_url or not token:
            with SessionLocal() as db:
                service = ConfigScanService(db)
                service.fail_run(run_id, "Missing GitLab credentials")
            logger.warning(
                "config_scan_run_failed run_id=%s product_id=%s context=manual_repo scope=%s reason=missing_gitlab_credentials",
                run_id,
                product_id,
                scope_path,
            )
            return

        await self._execute_scan_run(
            product_id=product_id,
            run_id=run_id,
            gitlab_url=gitlab_url,
            token=token,
            scope_path=scope_path,
            include_subgroups=False,
            log_context=f"manual_repo product_id={product_id} path={scope_path}",
        )

    async def _execute_manual_scan(
        self,
        product_id: int,
        run_id: int,
        scope_override: Optional[str] = None,
    ) -> None:
        with SessionLocal() as db:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                service = ConfigScanService(db)
                service.fail_run(run_id, "Product not found")
                return
            gitlab_url, token = self._resolve_instance_config(product)
            group_path = scope_override or self._extract_scope_path(product)

            from .models.config_scan import ConfigScanRun
            run_obj = db.query(ConfigScanRun).filter(ConfigScanRun.id == run_id).first()
            if run_obj and not run_obj.scope_path:
                run_obj.scope_path = group_path
                db.commit()

        if not gitlab_url or not token or not group_path:
            with SessionLocal() as db:
                service = ConfigScanService(db)
                service.fail_run(run_id, "Missing GitLab credentials or scope path")
            logger.warning(
                "config_scan_run_failed run_id=%s product_id=%s context=manual scope=%s reason=missing_gitlab_credentials_or_scope",
                run_id,
                product_id,
                group_path,
            )
            return

        await self._execute_scan_run(
            product_id=product_id,
            run_id=run_id,
            gitlab_url=gitlab_url,
            token=token,
            scope_path=group_path,
            include_subgroups=True,
            log_context=f"manual product_id={product_id}",
        )

    async def trigger_scan_by_url(
        self,
        gitlab_base_url: str,
        scan_path: str,
        product_id: int,
    ) -> int:
        with SessionLocal() as db:
            service = ConfigScanService(db)
            run = service.create_run(product_id, trigger="manual", scope_path=scan_path)
            run_id = run.id

        logger.info(
            "config_scan_queued trigger=url product_id=%s base_url=%s scope=%s run_id=%s include_subgroups=true",
            product_id,
            gitlab_base_url,
            scan_path,
            run_id,
        )
        asyncio.create_task(
            self._execute_scan_by_url(gitlab_base_url, scan_path, product_id, run_id)
        )
        return run_id

    async def _execute_scan_by_url(
        self,
        gitlab_base_url: str,
        scan_path: str,
        product_id: int,
        run_id: int,
    ) -> None:
        token = self.resolve_token_for_host(gitlab_base_url)
        if not token:
            with SessionLocal() as db:
                service = ConfigScanService(db)
                service.fail_run(run_id, "No GitLab token available for this host")
            logger.warning(
                "config_scan_run_failed run_id=%s product_id=%s context=url scope=%s base_url=%s reason=missing_gitlab_token",
                run_id,
                product_id,
                scan_path,
                gitlab_base_url,
            )
            return

        await self._execute_scan_run(
            product_id=product_id,
            run_id=run_id,
            gitlab_url=gitlab_base_url,
            token=token,
            scope_path=scan_path,
            include_subgroups=True,
            log_context=f"url scope={scan_path}",
        )

    def _instance_entry_for_netloc(self, netloc: str) -> Optional[Dict[str, str]]:
        if not netloc or not self.instance_map:
            return None
        key = netloc.lower().split("@")[-1].split(":")[0]
        for mapped_host, cfg in self.instance_map.items():
            hk = str(mapped_host).lower().split("@")[-1].split(":")[0]
            if hk == key:
                return cfg
        return None

    def resolve_token_for_host(self, gitlab_base_url: str) -> Optional[str]:
        # UI-managed connections can be saved after this poller was created.
        # Reload before every manual/worker resolution so a newly configured
        # GitLab host is usable immediately, without restarting the app.
        self._refresh_integration_config()
        u = (gitlab_base_url or "").strip().rstrip("/")
        parsed = urlparse(u if "://" in u else f"https://{u}")
        host = parsed.netloc or u
        cfg = self._instance_entry_for_netloc(host)
        if cfg:
            return cfg["token"]
        # Only hand GITLAB_PAT to the configured default instance — never to an
        # arbitrary host (a scan target is operator-supplied and could leak it).
        from .gitlab_url import is_known_gitlab_host

        if is_known_gitlab_host(u or host):
            return self.gitlab_pat
        logger.warning("Refusing to resolve GitLab token for unknown host %r", host)
        return None

    def _resolve_instance_config(self, product: Product) -> Tuple[Optional[str], Optional[str]]:
        """Pick API URL + token for GitLabScanner.

        Manual/poller must not use a bare namespace path as ``gitlab_base_url`` (some
        products only store scope in ``path``). Derive web origin from ``product.path`` when
        it is a full URL, same as scan-by-link.
        """
        self._refresh_integration_config()
        base_url = (product.gitlab_base_url or "").strip().rstrip("/")
        if base_url and looks_like_gitlab_web_url(base_url):
            parsed = urlparse(base_url)
            host = parsed.netloc
            cfg = self._instance_entry_for_netloc(host)
            if cfg:
                return cfg["api_url"], cfg["token"]
            if self.gitlab_pat:
                return base_url, self.gitlab_pat

        path_raw = (product.path or "").strip()
        if path_raw.startswith(("http://", "https://")):
            origin = gitlab_web_origin(path_raw)
            if origin:
                host = urlparse(origin).netloc
                cfg = self._instance_entry_for_netloc(host)
                if cfg:
                    return cfg["api_url"], cfg["token"]
                if self.gitlab_pat:
                    return origin, self.gitlab_pat

        default_api = runtime_setting("GITLAB_API_URL", "https://gitlab.com").rstrip("/")
        return (default_api, self.gitlab_pat) if self.gitlab_pat else (None, None)

    @staticmethod
    def _find_last_scope(db, product_id: int) -> Optional[str]:
        """Find scope_path from the last succeeded run. Handles legacy runs
        that predate the scope_path column by reading the report JSON."""
        from .models.config_scan import ConfigScanRun
        prev = (
            db.query(ConfigScanRun)
            .filter(
                ConfigScanRun.product_id == product_id,
                ConfigScanRun.status == "succeeded",
            )
            .order_by(ConfigScanRun.id.desc())
            .first()
        )
        if not prev:
            return None

        if prev.scope_path:
            return prev.scope_path

        if prev.report_json_path:
            try:
                with open(prev.report_json_path, "r") as f:
                    report = json.load(f)
                scope = (report.get("metadata") or {}).get("group_path")
                if scope:
                    prev.scope_path = scope
                    db.commit()
                    logger.info(
                        "Backfilled scope_path='%s' for run %s from report",
                        scope, prev.id,
                    )
                    return scope
            except Exception:
                logger.debug("Could not read report for run %s", prev.id)

        return None

    def _resolve_poller_scope(self, product: Product) -> Optional[str]:
        """Pick the best scope path for automatic polling.

        Priority:
        1. scope_path from the last succeeded run (known-good path)
        2. product.path — but only if it contains '/' (looks like a real
           GitLab namespace).  Bare names like 'platform' almost
           never resolve and just create failed runs.
        """
        with SessionLocal() as db:
            scope = self._find_last_scope(db, product.id)
        if scope:
            return scope

        fallback = self._extract_scope_path(product)
        if fallback and "/" in fallback:
            return fallback

        return None

    @staticmethod
    def _extract_scope_path(product: Product) -> str:
        raw = (product.path or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            raw = parsed.path or ""
        return raw.strip("/")

    @staticmethod
    def _load_instance_map(raw: str) -> Dict[str, Dict[str, str]]:
        raw = (raw or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse GITLAB_INSTANCES for config scan poller")
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
