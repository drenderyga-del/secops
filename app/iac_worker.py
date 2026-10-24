from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from .config_scan_poller import ConfigScanPoller
from .config_scanner.gitlab_client import GitLabAPIError, GitLabClient
from .database import DatabaseService
from .heartbeat import beat
from .iac_asoc import AsocClient, product_slug_for_path
from .iac_scanner import checkov_command, detect_iac_markers, parse_checkov_sarif
from .iac_service import severity_summary, summary_json
from .models import Product, Repository, SessionLocal
from .models.iac import IacScanFinding, IacScanRepository, IacScanRun

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class IacWorker:
    def __init__(self) -> None:
        self.enabled = os.getenv("IAC_WORKER_ENABLED", "true").lower() in {"1", "true", "yes"}
        self.interval = int(os.getenv("IAC_WORKER_INTERVAL_SECONDS", "10"))
        self.clone_timeout = int(os.getenv("IAC_CLONE_TIMEOUT_SECONDS", "600"))
        self.checkov_timeout = int(os.getenv("IAC_CHECKOV_TIMEOUT_SECONDS", "900"))
        self.gitlab_verify_ssl = os.getenv("IAC_GITLAB_VERIFY_SSL", os.getenv("CONFIG_SCAN_VERIFY_SSL", "true")).lower() in {"1", "true", "yes"}
        self.reports_dir = Path(os.getenv("IAC_REPORTS_DIR", "./iac_reports"))
        self.work_dir = Path(os.getenv("IAC_WORK_DIR", tempfile.gettempdir())) / "secpipe-iac"
        # ASOC upload is an optional post-scan step (findings live in the
        # monitor regardless); auto-creating ASOC products is off by default —
        # products are created by hand with the BI_ID in the name.
        self.asoc_upload_enabled = os.getenv("IAC_ASOC_UPLOAD_ENABLED", "true").lower() in {"1", "true", "yes"}
        self.config = ConfigScanPoller()
        self.asoc = AsocClient()

    @staticmethod
    def _observe_run(db, run: IacScanRun) -> None:
        from .iac_service import IacService

        IacService(db)._observe_execution(
            run,
            run.finished_at or run.started_at or datetime.now(timezone.utc),
        )

    async def run_forever(self) -> None:
        if not self.enabled:
            logger.warning("IaC worker disabled")
            return
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._preflight_tools()
        self._ensure_reports_dir_writable()
        # Recover runs THIS worker was executing before its restart (queued
        # rows stay queued — they are picked up by the loop below).
        from .iac_service import IacService

        with SessionLocal() as db:
            recovered = IacService(db).recover_interrupted_runs()
            if recovered:
                logger.warning("iac_worker_recovered_runs count=%s", recovered)
        logger.info(
            "iac_worker_start interval=%s clone_timeout=%s checkov_timeout=%s gitlab_verify_ssl=%s reports_dir=%s work_dir=%s",
            self.interval,
            self.clone_timeout,
            self.checkov_timeout,
            self.gitlab_verify_ssl,
            self.reports_dir,
            self.work_dir,
        )
        last_cleanup = 0.0
        while True:
            beat("iac_worker")
            # Age-based SARIF retention (~daily): DB retention prunes old
            # iac_scan_repositories rows but nothing deleted their files, so
            # the shared reports volume grew forever.
            if time.monotonic() - last_cleanup > 86400:
                self._cleanup_old_reports()
                last_cleanup = time.monotonic()
            run = self._claim_next_run()
            if run:
                await self._execute_run(run.id)
            else:
                await asyncio.sleep(self.interval)

    def _cleanup_old_reports(self) -> None:
        """Delete SARIF files older than SECPIPE_RETENTION_DAYS (0 = keep forever)."""
        try:
            days = int(os.getenv("SECPIPE_RETENTION_DAYS", "365"))
        except ValueError:
            days = 365
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        removed = 0
        try:
            for path in self.reports_dir.glob("*.sarif"):
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            logger.warning("iac_reports_cleanup_failed dir=%s", self.reports_dir, exc_info=True)
            return
        if removed:
            logger.info("iac_reports_cleanup removed=%s older_than_days=%s", removed, days)

    def _claim_next_run(self) -> IacScanRun | None:
        with SessionLocal() as db:
            # FOR UPDATE SKIP LOCKED: with 2+ worker replicas, a plain SELECT
            # would let both claim the same queued run and scan it twice.
            run = (
                db.query(IacScanRun)
                .filter(IacScanRun.status == "queued")
                .order_by(IacScanRun.id.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if not run:
                return None
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            db.add(run)
            self._observe_run(db, run)
            db.commit()
            db.refresh(run)
            logger.info(
                "iac_run_claimed run_id=%s scope=%s base_url=%s single_project_id=%s",
                run.id,
                run.scope_path,
                run.gitlab_base_url,
                run.single_project_id,
            )
            return run

    async def _execute_run(self, run_id: int) -> None:
        started = time.monotonic()
        with SessionLocal() as db:
            run = db.query(IacScanRun).filter(IacScanRun.id == run_id).first()
            if not run:
                logger.warning("iac_run_missing run_id=%s", run_id)
                return
            if run.status == "cancelled" or run.cancel_requested_at:
                self._finalize_cancelled_run(run_id)
                return
            token = self.config.resolve_token_for_host(run.gitlab_base_url)
            if not token:
                logger.warning(
                    "iac_run_failed run_id=%s scope=%s base_url=%s reason=missing_gitlab_token",
                    run_id,
                    run.scope_path,
                    run.gitlab_base_url,
                )
                run.status = "failed"
                run.finished_at = datetime.now(timezone.utc)
                run.error_text = "No GitLab token available for target host"
                self._observe_run(db, run)
                db.commit()
                return
            gitlab_base_url = run.gitlab_base_url
            scope_path = run.scope_path
            single_project_id = run.single_project_id

        try:
            logger.info(
                "iac_run_start run_id=%s scope=%s base_url=%s single_project_id=%s gitlab_verify_ssl=%s",
                run_id,
                scope_path,
                gitlab_base_url,
                single_project_id,
                self.gitlab_verify_ssl,
            )
            async with GitLabClient(
                gitlab_base_url,
                token,
                verify_ssl=self.gitlab_verify_ssl,
                request_timeout=int(os.getenv("IAC_GITLAB_REQUEST_TIMEOUT", "30")),
            ) as client:
                projects = await self._resolve_projects(client, scope_path, single_project_id)
                logger.info(
                    "iac_run_projects_resolved run_id=%s scope=%s total=%s mode=%s",
                    run_id,
                    scope_path,
                    len(projects),
                    "single_project" if single_project_id else "scope",
                )
                await self._store_total(run_id, len(projects))
                for project in projects:
                    if self._is_cancel_requested(run_id):
                        self._finalize_cancelled_run(run_id)
                        return
                    # A run over a big group takes hours; beat per project so
                    # liveness checks don't flag the worker as stale mid-run.
                    beat("iac_worker")
                    await self._process_project(run_id, client, token, gitlab_base_url, scope_path, project)
            if self._is_cancel_requested(run_id):
                self._finalize_cancelled_run(run_id)
                return
            self._complete_run(run_id)
            logger.info("iac_run_finish run_id=%s duration_ms=%s", run_id, int((time.monotonic() - started) * 1000))
        except Exception as exc:
            if self._is_cancel_requested(run_id):
                self._finalize_cancelled_run(run_id)
                return
            logger.exception("iac_run_failed run_id=%s scope=%s error=%s", run_id, scope_path, exc)
            with SessionLocal() as db:
                run = db.query(IacScanRun).filter(IacScanRun.id == run_id).first()
                if run:
                    run.status = "failed"
                    run.finished_at = datetime.now(timezone.utc)
                    run.error_text = str(exc)
                    self._observe_run(db, run)
                    db.commit()

    async def _resolve_projects(
        self,
        client: GitLabClient,
        scope_path: str,
        single_project_id: int | None,
    ) -> list[dict]:
        if single_project_id:
            # Single-repo rescan: scan only this project, skip group listing.
            logger.info("iac_resolve_projects_start mode=single_project project_id=%s", single_project_id)
            project = await client.get_project(single_project_id)
            return [project] if project else []

        try:
            logger.info("iac_resolve_projects_start mode=group scope=%s include_subgroups=true", scope_path)
            projects = await client.list_group_projects(scope_path, include_subgroups=True)
            logger.info("iac_resolve_projects_finish mode=group scope=%s total=%s", scope_path, len(projects))
            return projects
        except GitLabAPIError as group_error:
            logger.warning(
                "iac_resolve_projects_group_failed scope=%s fallback=project error=%s",
                scope_path,
                group_error,
            )
            try:
                project = await client.get_project(scope_path)
            except GitLabAPIError:
                raise group_error
            logger.info(
                "iac_resolve_projects_finish mode=project scope=%s total=%s",
                scope_path,
                1 if project else 0,
            )
            return [project] if project else []

    async def _store_total(self, run_id: int, total: int) -> None:
        with SessionLocal() as db:
            run = db.query(IacScanRun).filter(IacScanRun.id == run_id).first()
            if run:
                run.total_repositories = total
                db.commit()

    async def _process_project(
        self,
        run_id: int,
        client: GitLabClient,
        token: str,
        gitlab_base_url: str,
        scope_path: str,
        project: dict,
    ) -> None:
        repo_row_id = self._create_project_row(run_id, gitlab_base_url, scope_path, project)
        project_id = int(project["id"])
        default_branch = str(project.get("default_branch") or "")
        project_path = str(project.get("path_with_namespace") or project.get("path") or project_id)
        logger.info(
            "iac_project_start run_id=%s repo_row_id=%s project_id=%s project=%s default_branch=%s",
            run_id,
            repo_row_id,
            project_id,
            project_path,
            default_branch or "-",
        )
        if not default_branch:
            # An empty repository (no default branch → no files) has no IaC;
            # marking it failed kept whole group runs red forever.
            logger.info(
                "iac_project_empty run_id=%s repo_row_id=%s project=%s reason=no_default_branch",
                run_id,
                repo_row_id,
                project_path,
            )
            self._mark_repo(
                repo_row_id,
                finder_status="succeeded",
                scan_status="no_iac",
                asoc_upload_status="skipped",
                error_text="No default branch (empty repository)",
            )
            return
        try:
            tree = await client.list_repository_tree(project_id, default_branch, recursive=True)
            markers = detect_iac_markers(tree)
            logger.info(
                "iac_project_markers run_id=%s repo_row_id=%s project=%s files=%s markers=%s",
                run_id,
                repo_row_id,
                project_path,
                len(tree),
                markers,
            )
        except Exception as exc:
            logger.exception(
                "iac_project_tree_failed run_id=%s repo_row_id=%s project=%s branch=%s error=%s",
                run_id,
                repo_row_id,
                project_path,
                default_branch,
                exc,
            )
            self._mark_repo(repo_row_id, finder_status="failed", scan_status="failed", error_text=str(exc))
            return
        if not markers:
            logger.info(
                "iac_project_no_iac run_id=%s repo_row_id=%s project=%s",
                run_id,
                repo_row_id,
                project_path,
            )
            self._mark_repo(repo_row_id, finder_status="succeeded", scan_status="no_iac", asoc_upload_status="skipped")
            return

        self._mark_repo(
            repo_row_id,
            finder_status="succeeded",
            scan_status="running",
            has_iac=True,
            iac_markers=json.dumps(markers, ensure_ascii=False),
        )
        try:
            scan_started = time.monotonic()
            sarif_path = await asyncio.to_thread(
                self._clone_and_scan,
                run_id,
                project,
                token,
                default_branch,
            )
            findings = parse_checkov_sarif(sarif_path)
            logger.info(
                "iac_project_scan_finish run_id=%s repo_row_id=%s project=%s sarif=%s findings=%s duration_ms=%s",
                run_id,
                repo_row_id,
                project_path,
                sarif_path,
                len(findings),
                int((time.monotonic() - scan_started) * 1000),
            )
            self._save_findings(repo_row_id, sarif_path, findings)
            if self._is_cancel_requested(run_id):
                self._mark_repo(
                    repo_row_id,
                    asoc_upload_status="skipped",
                    error_text="ASOC upload skipped because run cancellation was requested",
                )
                return
            self._upload_asoc(repo_row_id, gitlab_base_url, scope_path)
        except Exception as exc:
            logger.exception(
                "iac_project_failed run_id=%s repo_row_id=%s project=%s stage=scan_or_upload error=%s",
                run_id,
                repo_row_id,
                project_path,
                exc,
            )
            self._mark_repo(repo_row_id, scan_status="failed", asoc_upload_status="skipped", error_text=str(exc))

    def _create_project_row(self, run_id: int, gitlab_base_url: str, scope_path: str, project: dict) -> int:
        path = str(project.get("path_with_namespace") or project.get("path") or project["id"]).strip("/")
        project_url = str(project.get("web_url") or f"{gitlab_base_url.rstrip('/')}/{path}")
        gitlab_stub = {"url": project_url, "project": {"path": path}}
        with SessionLocal() as db:
            db_service = DatabaseService(db)
            product = db_service.get_or_create_product(gitlab_stub)
            repo = db_service.get_or_create_repository(product, gitlab_stub)
            row = IacScanRepository(
                run_id=run_id,
                product_id=product.id,
                repository_id=repo.id,
                gitlab_project_id=int(project["id"]),
                project_path=path,
                project_url=project_url,
                default_branch=project.get("default_branch"),
                clone_url=project.get("http_url_to_repo") or project_url,
                finder_status="running",
                scan_status="queued",
                asoc_upload_status="pending",
                asoc_product_slug=product_slug_for_path(gitlab_base_url, path, scope_path),
            )
            db.add(row)
            db.flush()
            from .iac_service import IacService

            IacService(db).sync_repository_execution(row)
            db.commit()
            db.refresh(row)
            return row.id

    def _clone_and_scan(self, run_id: int, project: dict, token: str, branch: str) -> str:
        project_path = str(project.get("path_with_namespace") or project["id"]).strip("/")
        clone_url = str(project.get("http_url_to_repo") or project.get("web_url") or "")
        if not clone_url:
            raise RuntimeError("Project has no clone URL")
        self._ensure_reports_dir_writable()
        run_dir = self.work_dir / f"run-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = Path(tempfile.mkdtemp(prefix="repo-", dir=str(run_dir)))
        try:
            auth_url = self._clone_url_with_token(clone_url, token)
            logger.info(
                "iac_external_step_start run_id=%s project=%s step=git_clone branch=%s",
                run_id,
                project_path,
                branch,
            )
            self._run_step(
                "git clone",
                ["git", "clone", "--depth", "1", "--branch", branch, auth_url, str(repo_dir)],
                timeout=self.clone_timeout,
                redact=token,
            )
            safe_name = project_path.replace("/", "__")
            sarif_path = self.reports_dir / f"run-{run_id}-{safe_name}.sarif"
            checkov_output_dir = repo_dir / ".checkov-output"
            logger.info(
                "iac_external_step_start run_id=%s project=%s step=checkov output=%s",
                run_id,
                project_path,
                checkov_output_dir,
            )
            self._run_step(
                "checkov",
                checkov_command(str(checkov_output_dir)),
                cwd=str(repo_dir),
                timeout=self.checkov_timeout,
            )
            checkov_sarif = self._find_checkov_sarif_output(checkov_output_dir)
            if not checkov_sarif:
                raise RuntimeError(f"checkov did not create SARIF output in: {checkov_output_dir}")
            shutil.copyfile(checkov_sarif, sarif_path)
            if not sarif_path.is_file() or sarif_path.stat().st_size == 0:
                raise RuntimeError(f"checkov did not create SARIF output: {sarif_path}")
            logger.info(
                "iac_external_step_finish run_id=%s project=%s step=checkov output=%s size=%s",
                run_id,
                project_path,
                sarif_path,
                sarif_path.stat().st_size,
            )
            return str(sarif_path)
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)

    def _preflight_tools(self) -> None:
        missing = [tool for tool in ("git", os.getenv("IAC_CHECKOV_BIN", "checkov")) if not shutil.which(tool)]
        if missing:
            raise RuntimeError(f"IaC worker dependency missing: {', '.join(missing)}")

    def _ensure_reports_dir_writable(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        probe = self.reports_dir / ".write-test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"IAC_REPORTS_DIR is not writable: {self.reports_dir} ({exc})") from exc

    @staticmethod
    def _find_checkov_sarif_output(output_path: Path) -> Path | None:
        if output_path.is_file() and output_path.stat().st_size > 0:
            return output_path
        preferred = output_path / "results_sarif.sarif"
        if preferred.is_file() and preferred.stat().st_size > 0:
            return preferred
        if output_path.is_dir():
            for candidate in sorted(output_path.rglob("*.sarif")):
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
        return None

    @staticmethod
    def _run_step(label: str, command: list[str], *, timeout: int, cwd: str | None = None, redact: str | None = None) -> None:
        """Run an external command, surfacing stderr on failure.

        ``subprocess.run(check=True)`` raises ``CalledProcessError`` whose
        ``str()`` only reports the exit code — the actual cause (auth failure,
        missing branch, checkov crash) lives in stderr and was being dropped.
        This wrapper raises ``RuntimeError`` with the captured stderr so the
        reason ends up in ``IacScanRepository.error_text`` and the UI.
        """

        def _clean(text: str | None) -> str:
            text = (text or "").strip()
            if redact and text:
                text = text.replace(redact, "***")
            # Keep the tail: clone/checkov failures put the cause on the last lines.
            lines = [ln for ln in text.splitlines() if ln.strip()]
            return "\n".join(lines[-8:])

        try:
            subprocess.run(
                command,
                cwd=cwd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            detail = _clean(exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr)
            raise RuntimeError(f"{label} timed out after {timeout}s" + (f": {detail}" if detail else "")) from exc
        except subprocess.CalledProcessError as exc:
            detail = _clean(exc.stderr) or _clean(exc.stdout) or f"exit code {exc.returncode}"
            raise RuntimeError(f"{label} failed: {detail}") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} failed: executable not found ({command[0]})") from exc

    @staticmethod
    def _clone_url_with_token(clone_url: str, token: str) -> str:
        parsed = urlparse(clone_url)
        if parsed.scheme not in {"http", "https"} or not token:
            return clone_url
        quoted = quote(token, safe="")
        netloc = f"oauth2:{quoted}@{parsed.netloc}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    def _save_findings(self, repo_row_id: int, sarif_path: str, findings: list[dict]) -> None:
        counts = severity_summary(findings)
        with SessionLocal() as db:
            row = db.query(IacScanRepository).filter(IacScanRepository.id == repo_row_id).first()
            if not row:
                return
            run_id = row.run_id
            db.query(IacScanFinding).filter(IacScanFinding.scan_repository_id == row.id).delete()
            for finding in findings:
                db.add(
                    IacScanFinding(
                        run_id=row.run_id,
                        scan_repository_id=row.id,
                        product_id=row.product_id,
                        repository_id=row.repository_id,
                        project_path=row.project_path,
                        project_url=row.project_url,
                        severity=finding["severity"],
                        rule_id=finding["rule_id"],
                        title=finding["title"],
                        description=finding.get("description"),
                        recommendation=finding.get("recommendation"),
                        file_path=finding.get("file_path"),
                        line=finding.get("line"),
                        evidence=finding.get("evidence"),
                        scanner_name="checkov",
                    )
                )
            row.sarif_path = sarif_path
            row.critical_count = counts["critical"]
            row.high_count = counts["high"]
            row.medium_count = counts["medium"]
            row.low_count = counts["low"]
            row.info_count = counts["info"]
            row.findings_count = sum(counts.values())
            row.scan_status = "succeeded"
            artifact_path = Path(sarif_path)
            from .iac_service import IacService

            IacService(db).sync_repository_execution(
                row,
                artifact_digest_sha256=self._sha256_file(artifact_path),
                artifact_size_bytes=artifact_path.stat().st_size,
            )
            db.commit()
        self._refresh_run_counters(run_id)

    def _upload_asoc(self, repo_row_id: int, gitlab_base_url: str, scope_path: str) -> None:
        """Push the SARIF into an ASOC repository, creating it when absent.

        The repository is resolved by slug (<instance-prefix>/<project_path>) and, when
        that misses, by the GitLab URL (ASOC card ``url``/``externalLink.url``)
        — repositories often already exist under a legacy ci/* slug but carry
        the correct VCS binding. When neither lookup succeeds, ScanOps creates
        a repository with the GitLab URL and attaches IAC_HEXWAY_PRODUCT_ID
        when configured; ASOC products are never auto-created.
        """
        if not self.asoc_upload_enabled:
            with SessionLocal() as db:
                row = db.query(IacScanRepository).filter(IacScanRepository.id == repo_row_id).first()
                if row:
                    row.asoc_upload_status = "skipped"
                    from .iac_service import IacService

                    IacService(db).sync_repository_execution(row)
                    db.commit()
            return
        with SessionLocal() as db:
            row = db.query(IacScanRepository).filter(IacScanRepository.id == repo_row_id).first()
            if not row:
                return
            run_id = row.run_id
            upload = self.asoc.upload_repo_row(row, gitlab_base_url)
            from .iac_service import IacService

            IacService(db).sync_repository_execution(row)
            db.commit()
            logger.info(
                "iac_asoc_upload_finish run_id=%s repo_row_id=%s project=%s status=%s slug=%s matched_by=%s message=%s",
                run_id,
                repo_row_id,
                row.project_path,
                upload.status,
                row.asoc_repository_slug,
                upload.matched_by,
                upload.message,
            )
        self._refresh_run_counters(run_id)

    def _mark_repo(self, repo_row_id: int, **values) -> None:
        with SessionLocal() as db:
            row = db.query(IacScanRepository).filter(IacScanRepository.id == repo_row_id).first()
            if not row:
                return
            run_id = row.run_id
            for key, value in values.items():
                setattr(row, key, value)
            from .iac_service import IacService

            IacService(db).sync_repository_execution(row)
            db.commit()
        self._refresh_run_counters(run_id)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _refresh_run_counters(self, run_id: int) -> None:
        from .iac_service import IacService

        with SessionLocal() as db:
            IacService(db).refresh_run_counters(run_id)

    @staticmethod
    def _is_cancel_requested(run_id: int) -> bool:
        with SessionLocal() as db:
            run = db.query(IacScanRun).filter(IacScanRun.id == run_id).first()
            return bool(run and (run.status == "cancelled" or run.cancel_requested_at))

    @staticmethod
    def _finalize_cancelled_run(run_id: int) -> None:
        with SessionLocal() as db:
            run = db.query(IacScanRun).filter(IacScanRun.id == run_id).first()
            if not run or run.status == "cancelled":
                return
            now = datetime.now(timezone.utc)
            run.status = "cancelled"
            run.cancelled_at = now
            run.finished_at = now
            run.error_text = "Cancelled by user"
            IacWorker._observe_run(db, run)
            db.commit()

    def _complete_run(self, run_id: int) -> None:
        with SessionLocal() as db:
            run = db.query(IacScanRun).filter(IacScanRun.id == run_id).first()
            if not run:
                return
            rows = db.query(IacScanRepository).filter(IacScanRepository.run_id == run_id).all()
            run.candidate_repositories = sum(1 for r in rows if r.has_iac)
            run.scanned_repositories = sum(1 for r in rows if r.scan_status == "succeeded")
            run.uploaded_repositories = sum(1 for r in rows if r.asoc_upload_status == "uploaded")
            run.findings_count = sum(int(r.findings_count or 0) for r in rows)
            run.summary_json = summary_json(
                {
                    "repositories_total": run.total_repositories,
                    "iac_repositories": run.candidate_repositories,
                    "scanned_repositories": run.scanned_repositories,
                    "uploaded_repositories": run.uploaded_repositories,
                    "findings_total": run.findings_count,
                    "severity": {
                        "critical": sum(int(r.critical_count or 0) for r in rows),
                        "high": sum(int(r.high_count or 0) for r in rows),
                        "medium": sum(int(r.medium_count or 0) for r in rows),
                        "low": sum(int(r.low_count or 0) for r in rows),
                        "info": sum(int(r.info_count or 0) for r in rows),
                    },
                }
            )
            run.status = self._run_status_for_rows(rows)
            run.finished_at = datetime.now(timezone.utc)
            self._observe_run(db, run)
            db.commit()
            logger.info(
                "iac_run_completed run_id=%s status=%s total=%s candidates=%s scanned=%s uploaded=%s findings=%s",
                run.id,
                run.status,
                run.total_repositories,
                run.candidate_repositories,
                run.scanned_repositories,
                run.uploaded_repositories,
                run.findings_count,
            )

    @staticmethod
    def _run_status_for_rows(rows: list[IacScanRepository]) -> str:
        """Run verdict from its repo rows: succeeded / partial / failed.

        EVERY failed row counts, not only IaC candidates: finder failures
        (tree listing died — bad token, unreachable GitLab) leave has_iac
        False, and a run where every repo failed at the finder stage used to
        report "succeeded". A repo with no IaC is a benign success.
        """
        failed = sum(
            1
            for row in rows
            if row.scan_status == "failed"
            or (row.scan_status == "succeeded" and row.asoc_upload_status != "uploaded")
        )
        succeeded = sum(
            1
            for row in rows
            if row.scan_status == "no_iac"
            or (row.scan_status == "succeeded" and row.asoc_upload_status == "uploaded")
        )
        if not rows:
            return "succeeded"
        if failed and succeeded:
            return "partial"
        if failed:
            return "failed"
        return "succeeded"


async def main() -> None:
    await IacWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
