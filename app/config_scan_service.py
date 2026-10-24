from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .execution_contract import ExecutionStatus, LegacySource, StepStatus
from .execution_service import ExecutionService
from .models import Product, Repository
from .models.repository import RepositoryStatus
from .models.config_scan import ConfigScanRun, ConfigScanFinding

logger = logging.getLogger(__name__)


def project_counts_for_run(run: Optional[ConfigScanRun]) -> tuple[int, int]:
    """Return (scanned, total) for UI/API.

    For succeeded runs, prefer ``summary_json`` — it matches the persisted report and
    can differ from ORM columns if those were never updated or came from an older bug.
    """
    if not run:
        return 0, 0
    if run.status == "succeeded" and run.summary_json:
        try:
            data = json.loads(run.summary_json)
            pt = data.get("projects_total")
            ps = data.get("projects_scanned")
            if isinstance(pt, int) and isinstance(ps, int):
                return ps, pt
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return int(run.scanned_projects or 0), int(run.total_projects or 0)


class ConfigScanService:
    def __init__(self, db: Session):
        self.db = db

    def _observe_execution(self, run: ConfigScanRun, observed_at: datetime) -> None:
        ExecutionService(self.db).observe_legacy_execution(
            source=LegacySource.CONFIG_SCAN,
            raw_status=run.status,
            correlation_key=f"config_scan:{run.id}",
            repository_id=None,
            product_id=run.product_id,
            observed_at=observed_at,
        )

    # ── Run lifecycle ──────────────────────────────────────────────

    def create_run(
        self,
        product_id: int,
        trigger: str = "poller",
        scope_path: str | None = None,
    ) -> ConfigScanRun:
        run = ConfigScanRun(
            product_id=product_id,
            status="queued",
            trigger=trigger,
            scope_path=scope_path,
        )
        self.db.add(run)
        self.db.flush()
        self._observe_execution(run, datetime.now(timezone.utc))
        self.db.commit()
        self.db.refresh(run)
        return run

    def mark_run_started(self, run_id: int) -> None:
        run = self.db.query(ConfigScanRun).filter(ConfigScanRun.id == run_id).first()
        if run:
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            self._observe_execution(run, run.started_at)
            self.db.commit()

    def update_run_progress(
        self,
        run_id: int,
        total_projects: int,
        scanned_projects: int,
    ) -> None:
        run = self.db.query(ConfigScanRun).filter(ConfigScanRun.id == run_id).first()
        if run:
            run.total_projects = total_projects
            run.scanned_projects = scanned_projects
            self.db.commit()

    def complete_run(
        self,
        run_id: int,
        summary: dict[str, Any],
        findings_count: int,
        report_json_path: Optional[str] = None,
        report_html_path: Optional[str] = None,
    ) -> None:
        run = self.db.query(ConfigScanRun).filter(ConfigScanRun.id == run_id).first()
        if run:
            run.status = "succeeded"
            run.finished_at = datetime.now(timezone.utc)
            run.summary_json = json.dumps(summary)
            run.findings_count = findings_count
            pt = summary.get("projects_total")
            ps = summary.get("projects_scanned")
            if isinstance(pt, int):
                run.total_projects = pt
            if isinstance(ps, int):
                run.scanned_projects = ps
            if report_json_path:
                run.report_json_path = report_json_path
            if report_html_path:
                run.report_html_path = report_html_path
            self._observe_execution(run, run.finished_at)
            self.db.commit()

    def fail_run(self, run_id: int, error: str) -> None:
        run = self.db.query(ConfigScanRun).filter(ConfigScanRun.id == run_id).first()
        if run:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.error_text = error
            self._observe_execution(run, run.finished_at)
            self.db.commit()

    def recover_interrupted_runs(self) -> int:
        stuck = (
            self.db.query(ConfigScanRun)
            .filter(ConfigScanRun.status.in_(["queued", "running"]))
            .all()
        )
        if not stuck:
            return 0
        now = datetime.now(timezone.utc)
        for run in stuck:
            run.status = "failed"
            run.finished_at = now
            run.error_text = "Interrupted by app restart"
        self.db.commit()
        return len(stuck)

    # ── Repositories (unified with pipeline products) ─────────────

    def sync_repositories_from_config_projects(
        self,
        product_id: int,
        projects: list[dict[str, Any]],
    ) -> int:
        """Create Repository rows for every scanned GitLab project path so
        /products/{id} lists them even when no pipeline job was ever ingested."""
        created = 0
        for p in projects:
            path = (p.get("project_path") or p.get("path_with_namespace") or "").strip().strip("/")
            if not path:
                continue
            exists = (
                self.db.query(Repository)
                .filter(Repository.product_id == product_id)
                .filter(func.lower(Repository.path) == path.lower())
                .first()
            )
            if exists:
                continue
            self.db.add(
                Repository(
                    product_id=product_id,
                    path=path,
                    overall_status=RepositoryStatus.UNKNOWN,
                )
            )
            created += 1
        if created:
            self.db.commit()
            logger.info(
                "Config scan linked %s new repository row(s) to product_id=%s",
                created,
                product_id,
            )
        return created

    # ── Findings persistence ───────────────────────────────────────

    def save_findings(
        self,
        run_id: int,
        product_id: int,
        projects: list[dict[str, Any]],
    ) -> int:
        self.sync_repositories_from_config_projects(product_id, projects)
        repo_path_map = self._build_repo_path_map(product_id)
        total_findings = 0

        for project_data in projects:
            project_path = project_data.get("project_path", "")
            project_url = project_data.get("project_url", "")
            path_key = (project_path or "").strip().strip("/").lower()
            repo_id = repo_path_map.get(path_key)

            if repo_id is not None:
                observed_at = datetime.now(timezone.utc)
                lifecycle = ExecutionService(self.db)
                execution = lifecycle.observe_execution(
                    source=LegacySource.CONFIG_SCAN.value,
                    correlation_key=f"config_scan:{run_id}:repository:{repo_id}",
                    repository_id=repo_id,
                    product_id=product_id,
                    status=ExecutionStatus.SUCCEEDED,
                    observed_at=observed_at,
                )
                execution.context_json = json.dumps(
                    {
                        "findings_count": len(project_data.get("findings", [])),
                        "project_url": project_url or None,
                    }
                )
                step = lifecycle.observe_step(
                    execution,
                    source=LegacySource.CONFIG_SCAN.value,
                    external_id=f"config_scan:{run_id}:repository:{repo_id}",
                    name="Configuration evaluation",
                    status=StepStatus.SUCCEEDED,
                    observed_at=observed_at,
                )
                step.context_json = execution.context_json

            for f in project_data.get("findings", []):
                finding = ConfigScanFinding(
                    run_id=run_id,
                    product_id=product_id,
                    repository_id=repo_id,
                    project_path=project_path,
                    project_url=project_url,
                    severity=f.get("severity", "info"),
                    rule_id=f.get("code", "UNKNOWN"),
                    title=f.get("title", ""),
                    description=f.get("description"),
                    recommendation=f.get("recommendation"),
                    detail=f.get("evidence"),
                )
                self.db.add(finding)
                total_findings += 1

        self.db.commit()
        return total_findings

    def _build_repo_path_map(self, product_id: int) -> dict[str, int]:
        repos = (
            self.db.query(Repository)
            .filter(Repository.product_id == product_id)
            .all()
        )
        return {
            (repo.path or "").strip().strip("/").lower(): repo.id
            for repo in repos
            if repo.path
        }

    # ── Queries ────────────────────────────────────────────────────

    def get_runs_for_product(
        self,
        product_id: int,
        limit: int = 50,
    ) -> list[ConfigScanRun]:
        return (
            self.db.query(ConfigScanRun)
            .filter(ConfigScanRun.product_id == product_id)
            .order_by(ConfigScanRun.id.desc())
            .limit(limit)
            .all()
        )

    def get_latest_run_per_product(self) -> dict[int, ConfigScanRun]:
        subq = (
            self.db.query(
                ConfigScanRun.product_id,
                func.max(ConfigScanRun.id).label("max_id"),
            )
            .group_by(ConfigScanRun.product_id)
            .subquery()
        )
        rows = (
            self.db.query(ConfigScanRun)
            .join(subq, ConfigScanRun.id == subq.c.max_id)
            .all()
        )
        return {run.product_id: run for run in rows}

    def get_latest_succeeded_run_per_product(self) -> dict[int, ConfigScanRun]:
        subq = (
            self.db.query(
                ConfigScanRun.product_id,
                func.max(ConfigScanRun.id).label("max_id"),
            )
            .filter(ConfigScanRun.status == "succeeded")
            .group_by(ConfigScanRun.product_id)
            .subquery()
        )
        rows = (
            self.db.query(ConfigScanRun)
            .join(subq, ConfigScanRun.id == subq.c.max_id)
            .all()
        )
        return {run.product_id: run for run in rows}

    def get_run_by_id(self, run_id: int) -> Optional[ConfigScanRun]:
        return self.db.query(ConfigScanRun).filter(ConfigScanRun.id == run_id).first()

    def get_last_succeeded_run(self, product_id: int) -> Optional[ConfigScanRun]:
        return (
            self.db.query(ConfigScanRun)
            .filter(
                ConfigScanRun.product_id == product_id,
                ConfigScanRun.status == "succeeded",
            )
            .order_by(ConfigScanRun.id.desc())
            .first()
        )

    def load_report_for_run(self, run: ConfigScanRun) -> Optional[dict[str, Any]]:
        from .config_scan_reporting import load_report_json_file

        if not run.report_json_path:
            return None
        return load_report_json_file(Path(run.report_json_path))

    @staticmethod
    def parse_summary_json(run: Optional[ConfigScanRun]) -> Optional[dict[str, Any]]:
        if not run or not run.summary_json:
            return None
        try:
            return json.loads(run.summary_json)
        except (json.JSONDecodeError, TypeError):
            return None

    def severity_breakdown_from_run(self, run: Optional[ConfigScanRun]) -> Optional[dict[str, int]]:
        data = self.parse_summary_json(run)
        if not data:
            return None
        sev = data.get("severity")
        return sev if isinstance(sev, dict) else None

    @staticmethod
    def _empty_severity_summary() -> dict[str, int]:
        return {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}

    def summarize_product_overview_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        rows = bundle.get("repo_table_rows") or []
        severity = self._empty_severity_summary()
        projects_with_findings = 0
        findings_total = 0

        for row in rows:
            fsum = row.get("findings_summary") or {}
            row_total = int(row.get("findings_count", 0) or 0)
            findings_total += row_total
            if row_total > 0:
                projects_with_findings += 1
            for key in severity:
                severity[key] += int(fsum.get(key, 0) or 0)

        projects_scanned = len(rows)
        return {
            "severity": severity,
            "projects_scanned": projects_scanned,
            "projects_total": projects_scanned,
            "projects_with_findings": projects_with_findings,
            "findings_total": findings_total,
        }

    def get_product_overview_summary(
        self,
        product: Product,
        *,
        max_runs: int = 200,
    ) -> dict[str, Any]:
        bundle = self.build_product_overview_bundle(product, max_runs=max_runs)
        return self.summarize_product_overview_bundle(bundle)

    def get_products_config_scan_compact(self) -> dict[int, dict[str, Any]]:
        """Per-product config-scan totals for the products LIST page, in 2 queries.

        Replaces the per-product ``get_product_overview_summary`` (which parses up
        to ``max_runs`` JSON report files from disk per product — an N+1 that made
        ``/products`` take seconds). Severity is summed from ``config_scan_findings``
        keeping the same "newest succeeded run per repository" dedup as the bundle,
        so manual one-repo scans still supplement the latest full scan.
        """
        # Q1: latest succeeded run per product → scanned_projects (the "was scanned" gate).
        latest_run = (
            self.db.query(
                ConfigScanRun.product_id.label("product_id"),
                func.max(ConfigScanRun.id).label("run_id"),
            )
            .filter(ConfigScanRun.status == "succeeded")
            .group_by(ConfigScanRun.product_id)
            .subquery()
        )
        result: dict[int, dict[str, Any]] = {}
        for product_id, scanned in (
            self.db.query(ConfigScanRun.product_id, ConfigScanRun.scanned_projects)
            .join(latest_run, ConfigScanRun.id == latest_run.c.run_id)
            .all()
        ):
            result[product_id] = {
                "projects_scanned": int(scanned or 0),
                "findings_total": 0,
                "severity": self._empty_severity_summary(),
            }

        # Q2: for each repository keep its newest SUCCEEDED run's findings, then sum
        # severities. Restricting to succeeded runs matches the overview bundle and
        # avoids counting findings from failed/partial runs.
        latest_repo_run = (
            self.db.query(
                ConfigScanFinding.product_id.label("product_id"),
                ConfigScanFinding.project_path.label("project_path"),
                func.max(ConfigScanFinding.run_id).label("run_id"),
            )
            .join(ConfigScanRun, ConfigScanFinding.run_id == ConfigScanRun.id)
            .filter(ConfigScanRun.status == "succeeded")
            .group_by(ConfigScanFinding.product_id, ConfigScanFinding.project_path)
            .subquery()
        )
        sev_rows = (
            self.db.query(
                ConfigScanFinding.product_id,
                ConfigScanFinding.severity,
                func.count().label("cnt"),
            )
            .join(
                latest_repo_run,
                and_(
                    ConfigScanFinding.product_id == latest_repo_run.c.product_id,
                    ConfigScanFinding.project_path == latest_repo_run.c.project_path,
                    ConfigScanFinding.run_id == latest_repo_run.c.run_id,
                ),
            )
            .group_by(ConfigScanFinding.product_id, ConfigScanFinding.severity)
            .all()
        )
        for product_id, severity, cnt in sev_rows:
            entry = result.setdefault(
                product_id,
                {"projects_scanned": 0, "findings_total": 0, "severity": self._empty_severity_summary()},
            )
            count = int(cnt or 0)
            entry["findings_total"] += count
            key = (severity or "").strip().lower()
            if key in entry["severity"]:
                entry["severity"][key] += count
        # Findings exist but no succeeded-run counter → still mark as scanned so the
        # severity pills render instead of a bare count.
        for entry in result.values():
            if entry["findings_total"] and not entry["projects_scanned"]:
                entry["projects_scanned"] = 1
        return result

    def build_results_bundle(
        self,
        product: Product,
        run: ConfigScanRun,
        *,
        report: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Shared JSON-report vs DB-only rendering for run / product pages."""
        from . import config_scan_reporting as csr

        if report is None:
            report = self.load_report_for_run(run)
        scope = csr.scope_path_for_run(product, run, report)
        raw = (report.get("projects") or []) if report else []
        if raw:
            tree = csr.build_project_tree(raw, scope)
            rows = csr.flat_repo_rows_from_projects(raw)
            groups = csr.group_repo_rows_by_first_segment(rows)
            top = csr.collect_top_findings_from_projects(raw, limit=100)
            return {
                "cscan_scope_path": scope,
                "project_tree": tree,
                "repo_table_rows": rows,
                "repo_table_groups": groups,
                "repo_table_meta": {
                    "repos": len(rows),
                    "findings": sum(r["findings_count"] for r in rows),
                },
                "project_groups": [],
                "top_findings": top,
            }

        pg = self.get_findings_grouped_by_project(run.id)
        return {
            "cscan_scope_path": scope,
            "project_tree": None,
            "repo_table_rows": [],
            "repo_table_groups": [],
            "repo_table_meta": {"repos": 0, "findings": 0},
            "project_groups": pg,
            "top_findings": [],
        }

    def build_product_overview_bundle(self, product: Product, *, max_runs: int = 200) -> dict[str, Any]:
        """Aggregate a product-wide view across succeeded runs.

        For each repository path we keep the newest succeeded run entry, so one-off
        manual repo scans do not hide findings from other repositories.
        """
        from . import config_scan_reporting as csr

        scope = csr.gitlab_path_from_product_field(product.path) or (product.name or "").strip() or "unknown"
        runs = (
            self.db.query(ConfigScanRun)
            .filter(
                ConfigScanRun.product_id == product.id,
                ConfigScanRun.status == "succeeded",
            )
            .order_by(ConfigScanRun.id.desc())
            .limit(max_runs)
            .all()
        )
        if not runs:
            return {
                "cscan_scope_path": scope,
                "project_tree": None,
                "repo_table_rows": [],
                "repo_table_groups": [],
                "repo_table_meta": {"repos": 0, "findings": 0},
                "project_groups": [],
                "top_findings": [],
            }

        newest_by_repo: dict[str, dict[str, Any]] = {}
        for run in runs:
            # Primary source: report projects (includes zero-findings repos).
            report = self.load_report_for_run(run)
            projects = (report.get("projects") or []) if report else []
            for p in projects:
                project_path = str(p.get("project_path") or "").strip().strip("/")
                if not project_path:
                    continue
                key = project_path.lower()
                if key in newest_by_repo:
                    continue
                newest_by_repo[key] = {
                    **p,
                    "project_path": project_path,
                    "findings_run_id": run.id,
                }

            # Secondary source: DB findings for this run (legacy fallback + summary truth).
            db_rows = self.get_findings_grouped_by_project(run.id)
            by_path = {
                (str(g.get("project_path") or "").strip().strip("/").lower()): g
                for g in db_rows
                if str(g.get("project_path") or "").strip().strip("/")
            }
            for key, g in by_path.items():
                if key not in newest_by_repo:
                    project_path = str(g.get("project_path") or "").strip().strip("/")
                    findings = g.get("findings", []) or []
                    normalized_findings = [
                        {
                            "severity": getattr(f, "severity", None),
                            "code": getattr(f, "rule_id", None),
                            "title": getattr(f, "title", None),
                            "description": getattr(f, "description", None),
                            "recommendation": getattr(f, "recommendation", None),
                            "evidence": getattr(f, "detail", None),
                        }
                        for f in findings
                    ]
                    newest_by_repo[key] = {
                        "project_path": project_path,
                        "project_url": g.get("project_url"),
                        "findings": normalized_findings,
                        "findings_summary": g.get("summary", {}),
                        "warnings": [],
                        "warnings_count": 0,
                        "findings_run_id": run.id,
                    }
                elif newest_by_repo[key].get("findings_run_id") == run.id:
                    # Keep project from this run's report, but trust this same run's DB counters.
                    # Older DB rows must not overwrite a newer zero-finding report for the repo.
                    newest_by_repo[key]["findings_summary"] = g.get("summary", {})
                    newest_by_repo[key]["findings_count"] = len(g.get("findings", []) or [])

        if not newest_by_repo:
            # Legacy fallback for runs without report files and without persisted findings.
            for run in runs:
                for g in self.get_findings_grouped_by_project(run.id):
                    project_path = str(g.get("project_path") or "").strip().strip("/")
                    if not project_path:
                        continue
                    key = project_path.lower()
                    if key in newest_by_repo:
                        continue
                    findings = g.get("findings", []) or []
                    normalized_findings = [
                        {
                            "severity": getattr(f, "severity", None),
                            "code": getattr(f, "rule_id", None),
                            "title": getattr(f, "title", None),
                            "description": getattr(f, "description", None),
                            "recommendation": getattr(f, "recommendation", None),
                            "evidence": getattr(f, "detail", None),
                        }
                        for f in findings
                    ]
                    newest_by_repo[key] = {
                        "project_path": project_path,
                        "project_url": g.get("project_url"),
                        "findings": normalized_findings,
                        "findings_summary": g.get("summary", {}),
                        "warnings": [],
                        "findings_run_id": run.id,
                    }

        projects = list(newest_by_repo.values())
        if not projects:
            return {
                "cscan_scope_path": scope,
                "project_tree": None,
                "repo_table_rows": [],
                "repo_table_groups": [],
                "repo_table_meta": {"repos": 0, "findings": 0},
                "project_groups": [],
                "top_findings": [],
            }

        tree = csr.build_project_tree(projects, scope)
        rows = csr.flat_repo_rows_from_projects(projects)
        groups = csr.group_repo_rows_by_first_segment(rows)
        top = csr.collect_top_findings_from_projects(projects, limit=100)
        return {
            "cscan_scope_path": scope,
            "project_tree": tree,
            "repo_table_rows": rows,
            "repo_table_groups": groups,
            "repo_table_meta": {
                "repos": len(rows),
                "findings": sum(r["findings_count"] for r in rows),
            },
            "project_groups": [],
            "top_findings": top,
        }

    def resolve_project_findings_payload(
        self,
        run_id: int,
        project_query: str,
    ) -> Optional[dict[str, Any]]:
        from . import config_scan_reporting as csr

        run = self.get_run_by_id(run_id)
        if not run:
            return None
        report = self.load_report_for_run(run)
        return csr.resolve_project_findings_blob(self.db, run, report, project_query)

    def get_findings_for_run(
        self,
        run_id: int,
        severity: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ConfigScanFinding], int]:
        query = self.db.query(ConfigScanFinding).filter(ConfigScanFinding.run_id == run_id)
        if severity:
            query = query.filter(ConfigScanFinding.severity == severity)
        total = query.count()
        findings = (
            query.order_by(ConfigScanFinding.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return findings, total

    def get_product_findings_summary(self, product_id: int) -> dict[str, int]:
        product = self.db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return self._empty_severity_summary()
        overview = self.get_product_overview_summary(product)
        return overview["severity"]

    def get_all_runs(self, limit: int = 100) -> list[ConfigScanRun]:
        return (
            self.db.query(ConfigScanRun)
            .order_by(ConfigScanRun.id.desc())
            .limit(limit)
            .all()
        )

    def get_findings_grouped_by_project(self, run_id: int) -> list[dict[str, Any]]:
        findings = (
            self.db.query(ConfigScanFinding)
            .filter(ConfigScanFinding.run_id == run_id)
            .order_by(ConfigScanFinding.project_path, ConfigScanFinding.id)
            .all()
        )
        grouped: dict[str, dict[str, Any]] = {}
        for f in findings:
            key = f.project_path
            if key not in grouped:
                grouped[key] = {
                    "project_path": f.project_path,
                    "project_url": f.project_url,
                    "findings": [],
                    "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                }
            grouped[key]["findings"].append(f)
            grouped[key]["summary"][f.severity] = grouped[key]["summary"].get(f.severity, 0) + 1

        result = sorted(
            grouped.values(),
            key=lambda g: sum(g["summary"].values()),
            reverse=True,
        )
        return result

    def get_latest_succeeded_config_scan_repo_map(
        self,
        product_id: int,
    ) -> tuple[dict[str, dict[str, Any]], Optional[int]]:
        """Map normalized project_path -> summary + newest run for each repo."""
        product = self.db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return {}, None
        run = (
            self.db.query(ConfigScanRun)
            .filter(
                ConfigScanRun.product_id == product_id,
                ConfigScanRun.status == "succeeded",
            )
            .order_by(ConfigScanRun.id.desc())
            .first()
        )
        if not run:
            return {}, None
        by_path: dict[str, dict[str, Any]] = {}
        bundle = self.build_product_overview_bundle(product)
        for row in bundle.get("repo_table_rows") or []:
            key = (row.get("project_path") or "").strip().strip("/").lower()
            if not key:
                continue
            run_id = row.get("findings_run_id") or run.id
            by_path[key] = {
                "summary": row.get("findings_summary") or self._empty_severity_summary(),
                "total": int(row.get("findings_count", 0) or 0),
                "run_id": run_id,
                "project_url": row.get("project_url"),
            }
        return by_path, run.id
