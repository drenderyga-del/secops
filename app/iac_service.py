from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from .artifact_delivery_service import ArtifactDeliveryService, DeliveryStatus
from .execution_contract import (
    ExecutionStatus,
    FailureCategory,
    FailureReason,
    LegacySource,
    TERMINAL_EXECUTION_STATUSES,
)
from .execution_service import ExecutionService
from .iac_scanner import parse_checkov_sarif
from .models import Execution, Product, Repository
from .models.artifact import Artifact, Delivery
from .models.iac import IacScanFinding, IacScanRepository, IacScanRun

IMPORTANT_SEVERITIES = {"critical", "high"}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class IacService:
    def __init__(self, db: Session):
        self.db = db

    def _observe_execution(
        self,
        run: IacScanRun,
        observed_at: datetime,
        *,
        repository_id: int | None = None,
        product_id: int | None = None,
    ) -> None:
        lifecycle = ExecutionService(self.db)
        if run.status == "succeeded":
            lifecycle.observe_execution(
                source=LegacySource.IAC_RUN.value,
                correlation_key=f"iac_run:{run.id}",
                repository_id=repository_id,
                product_id=product_id,
                status=ExecutionStatus.SUCCEEDED,
                observed_at=observed_at,
            )
        else:
            lifecycle.observe_legacy_execution(
                source=LegacySource.IAC_RUN,
                raw_status=run.status,
                correlation_key=f"iac_run:{run.id}",
                repository_id=repository_id,
                product_id=product_id,
                observed_at=observed_at,
            )

    def sync_repository_execution(
        self,
        row: IacScanRepository,
        *,
        artifact_digest_sha256: str | None = None,
        artifact_size_bytes: int | None = None,
        observed_at: datetime | None = None,
        delivery_attempt_key: str = "initial",
    ) -> Execution:
        """Project one IaC repository row into the shared end-to-end contract."""
        observed_at = observed_at or datetime.now(timezone.utc)
        lifecycle = ExecutionService(self.db)
        correlation_key = f"iac_repository:{row.run_id}:{row.gitlab_project_id}"
        execution = self.db.query(Execution).filter(
            Execution.source == "iac_repository",
            Execution.source_scope == "default",
            Execution.correlation_key == correlation_key,
        ).first()
        if execution is None:
            execution = lifecycle.observe_execution(
                source="iac_repository",
                correlation_key=correlation_key,
                repository_id=row.repository_id,
                product_id=row.product_id,
                status=ExecutionStatus.RUNNING,
                observed_at=observed_at,
            )

        for source, raw_status, external_id, name in (
            (LegacySource.IAC_FINDER, row.finder_status, f"iac_repository:{row.id}:finder", "IaC discovery"),
            (LegacySource.IAC_SCAN, row.scan_status, f"iac_repository:{row.id}:scan", "Checkov scan"),
        ):
            lifecycle.observe_legacy_step(
                execution,
                source=source,
                raw_status=raw_status,
                external_id=external_id,
                name=name,
                observed_at=observed_at,
            )

        contract = ArtifactDeliveryService(self.db)
        if row.scan_status == "no_iac":
            return contract.finalize_execution(
                execution,
                delivery_required=False,
                not_applicable=True,
                observed_at=observed_at,
            )
        if row.scan_status == "failed":
            execution = lifecycle.observe_execution(
                source=execution.source,
                correlation_key=execution.correlation_key,
                repository_id=execution.repository_id,
                product_id=execution.product_id,
                status=ExecutionStatus.FAILED,
                observed_at=observed_at,
            )
            execution.failure_category = FailureCategory.EXECUTION.value
            execution.failure_reason = FailureReason.SCANNER_FAILED.value
            self.db.flush()
            return execution

        artifact_external_id = f"iac_repository:{row.id}:sarif"
        artifact = self.db.query(Artifact).filter(
            Artifact.execution_id == execution.id,
            Artifact.source == "checkov",
            Artifact.external_id == artifact_external_id,
        ).first()
        if artifact is None and artifact_digest_sha256 is None and row.sarif_path:
            artifact_path = Path(row.sarif_path)
            if artifact_path.is_file() and artifact_path.stat().st_size > 0:
                parse_checkov_sarif(artifact_path)
                digest = hashlib.sha256()
                with artifact_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                artifact_digest_sha256 = digest.hexdigest()
                artifact_size_bytes = artifact_path.stat().st_size
        if artifact_digest_sha256 is not None and artifact_size_bytes is not None:
            artifact = contract.record_artifact(
                execution=execution,
                source="checkov",
                external_id=artifact_external_id,
                kind="sarif",
                uri=row.sarif_path or "",
                digest_sha256=artifact_digest_sha256,
                size_bytes=artifact_size_bytes,
                schema_version="2.1.0",
                observed_at=observed_at,
            )
            contract.record_provenance(
                artifact=artifact,
                execution=execution,
                source_revision=None,
                source_ref=row.default_branch,
                observed_at=observed_at,
            )

        if artifact is None:
            if row.scan_status == "succeeded" and row.asoc_upload_status in {"uploaded", "failed", "skipped"}:
                return contract.finalize_execution(
                    execution,
                    delivery_required=True,
                    required_delivery_source="asoc",
                    observed_at=observed_at,
                )
            return execution

        delivery_status = {
            "pending": DeliveryStatus.REQUESTED,
            "uploaded": DeliveryStatus.CONFIRMED,
            "failed": DeliveryStatus.FAILED,
            "skipped": DeliveryStatus.SKIPPED,
        }[row.asoc_upload_status]
        lifecycle.observe_legacy_step(
            execution,
            source=LegacySource.ASOC_DELIVERY,
            raw_status=row.asoc_upload_status,
            external_id=f"iac_repository:{row.id}:asoc:{delivery_attempt_key}",
            name="ASOC delivery",
            observed_at=observed_at,
        )
        contract.observe_delivery(
            artifact=artifact,
            source="asoc",
            destination="asoc",
            status=delivery_status,
            external_id=row.asoc_repository_id,
            observed_at=observed_at,
            error_text=row.error_text if delivery_status is DeliveryStatus.FAILED else None,
            attempt_key=delivery_attempt_key,
        )
        if delivery_status in {DeliveryStatus.CONFIRMED, DeliveryStatus.FAILED, DeliveryStatus.SKIPPED}:
            if ExecutionStatus(execution.status) in TERMINAL_EXECUTION_STATUSES:
                return execution
            return contract.finalize_execution(
                execution,
                delivery_required=True,
                required_delivery_source="asoc",
                observed_at=observed_at,
            )
        return execution

    def prepare_delivery_attempt(self, row: IacScanRepository, *, delivery_attempt_key: str) -> bool:
        """Durably create one requested attempt before the external upload."""
        correlation_key = f"iac_repository:{row.run_id}:{row.gitlab_project_id}"
        execution = self.db.query(Execution).filter(
            Execution.source == "iac_repository",
            Execution.source_scope == "default",
            Execution.correlation_key == correlation_key,
        ).first()
        if execution is not None:
            existing = (
                self.db.query(Delivery)
                .join(Artifact, Artifact.id == Delivery.artifact_id)
                .filter(
                    Artifact.execution_id == execution.id,
                    Delivery.source == "asoc",
                    Delivery.attempt_key == delivery_attempt_key,
                )
                .first()
            )
            if existing is not None:
                return False
        previous_status = row.asoc_upload_status
        row.asoc_upload_status = "pending"
        try:
            self.sync_repository_execution(row, delivery_attempt_key=delivery_attempt_key)
        except Exception:
            row.asoc_upload_status = previous_status
            raise
        self.db.flush()
        return True

    def create_run(self, *, target_url: str, gitlab_base_url: str, scope_path: str) -> IacScanRun:
        """Queue a group scan; an identical queued/running run is returned
        instead of queueing a duplicate (double-submit of an hours-long scan)."""
        existing = (
            self._active_runs_query()
            .filter(IacScanRun.single_project_id.is_(None))
            .filter(IacScanRun.gitlab_base_url == gitlab_base_url)
            .filter(IacScanRun.scope_path == scope_path)
            .first()
        )
        if existing:
            return existing
        run = IacScanRun(
            status="queued",
            trigger="manual",
            target_url=target_url,
            gitlab_base_url=gitlab_base_url,
            scope_path=scope_path,
            scanner_name="checkov",
        )
        self.db.add(run)
        self.db.flush()
        self._observe_execution(run, datetime.now(timezone.utc))
        self.db.commit()
        self.db.refresh(run)
        return run

    def _active_runs_query(self):
        return (
            self.db.query(IacScanRun)
            .filter(IacScanRun.status.in_(["queued", "running"]))
            .order_by(IacScanRun.id.desc())
        )

    def get_run(self, run_id: int) -> IacScanRun | None:
        return self.db.query(IacScanRun).filter(IacScanRun.id == run_id).first()

    def cancel_run(self, run_id: int) -> IacScanRun | None:
        """Cancel a queued run immediately or request a cooperative stop.

        The worker owns the transition from ``running`` to ``cancelled`` after
        it reaches a safe boundary between repositories.
        """
        run = self.get_run(run_id)
        if not run:
            return None
        now = datetime.now(timezone.utc)
        if run.status == "queued":
            run.status = "cancelled"
            run.cancel_requested_at = run.cancel_requested_at or now
            run.cancelled_at = run.cancelled_at or now
            run.finished_at = run.finished_at or now
            run.error_text = "Cancelled by user before worker start"
        elif run.status == "running":
            run.cancel_requested_at = run.cancel_requested_at or now
            run.error_text = "Cancellation requested by user"
        elif run.status == "cancelled":
            pass
        else:
            raise ValueError(f"Run {run_id} cannot be cancelled from status {run.status}")
        if run.status == "cancelled":
            self._observe_execution(run, run.finished_at or now)
        self.db.commit()
        self.db.refresh(run)
        return run

    def create_single_repo_run(self, repo_row: IacScanRepository) -> IacScanRun:
        """Queue a rescan of just one repository (its own lightweight run).

        A queued/running rescan of the same GitLab project is returned as is —
        double-clicking Rescan must not stack identical runs."""
        existing = (
            self._active_runs_query()
            .filter(IacScanRun.single_project_id == repo_row.gitlab_project_id)
            .first()
        )
        if existing:
            return existing
        parent = self.get_run(repo_row.run_id)
        # scope_path feeds the ASOC product slug: keep the parent group scope
        # so the rescan row gets the same slug as the original scan; without a
        # parent, fall back to the repo's own parent group.
        fallback_scope = repo_row.project_path.rsplit("/", 1)[0] if "/" in repo_row.project_path else repo_row.project_path
        run = IacScanRun(
            status="queued",
            trigger="rescan",
            target_url=repo_row.project_url or (parent.target_url if parent else ""),
            gitlab_base_url=(parent.gitlab_base_url if parent else ""),
            scope_path=(parent.scope_path if parent else fallback_scope),
            single_project_id=repo_row.gitlab_project_id,
            scanner_name="checkov",
        )
        self.db.add(run)
        self.db.flush()
        self._observe_execution(
            run,
            datetime.now(timezone.utc),
            repository_id=repo_row.repository_id,
            product_id=repo_row.product_id,
        )
        self.db.commit()
        self.db.refresh(run)
        return run

    def list_runs(self, limit: int = 50) -> list[IacScanRun]:
        return self.db.query(IacScanRun).order_by(IacScanRun.id.desc()).limit(limit).all()

    def recover_interrupted_runs(self) -> int:
        """Fail runs that were mid-execution when the IaC WORKER restarted.

        Called from the worker's own startup — not from the web app: IaC runs
        are executed by a separate container, so a web deploy/restart says
        nothing about their health. Only ``running`` rows are failed (this
        worker was executing them and clearly is not anymore); ``queued`` rows
        stay queued — the worker is about to pick them up.
        """
        runs = self.db.query(IacScanRun).filter(IacScanRun.status == "running").all()
        if not runs:
            return 0
        now = datetime.now(timezone.utc)
        for run in runs:
            run.status = "failed"
            run.finished_at = now
            run.error_text = "Interrupted by IaC worker restart"
            self._observe_execution(run, now)
        self.db.commit()
        return len(runs)

    def refresh_run_counters(self, run_id: int) -> None:
        run = self.get_run(run_id)
        if not run:
            return
        rows = self.db.query(IacScanRepository).filter(IacScanRepository.run_id == run_id).all()
        run.candidate_repositories = sum(1 for r in rows if r.has_iac)
        run.scanned_repositories = sum(1 for r in rows if r.scan_status == "succeeded")
        run.uploaded_repositories = sum(1 for r in rows if r.asoc_upload_status == "uploaded")
        run.findings_count = sum(int(r.findings_count or 0) for r in rows)
        self.db.commit()

    def latest_repo_rows_query(self):
        subq = (
            self.db.query(
                IacScanRepository.project_path.label("project_path"),
                func.max(IacScanRepository.id).label("max_id"),
            )
            .join(IacScanRun, IacScanRun.id == IacScanRepository.run_id)
            # Any FINISHED run counts, not only "succeeded": a partial group
            # run still produced fresh per-repo state, and a failed single-repo
            # rescan must surface instead of silently keeping the old success.
            # Unfinished runs stay excluded (their rows are still moving).
            .filter(IacScanRun.status.in_(["succeeded", "partial", "failed"]))
            .filter(IacScanRepository.scan_status.in_(["succeeded", "no_iac", "failed"]))
            .group_by(IacScanRepository.project_path)
            .subquery()
        )
        return self.db.query(IacScanRepository).join(subq, IacScanRepository.id == subq.c.max_id)

    def latest_repo_rows(
        self,
        limit: int = 500,
        offset: int = 0,
        query_text: str = "",
        iac: str = "all",
        scan_status: str = "",
        asoc_status: str = "",
        has_important_findings: bool = False,
    ) -> tuple[list[IacScanRepository], int]:
        """Return a page from the latest finished state, optionally filtered.

        All predicates are applied after the latest-row-per-project subquery so
        a filter never exposes an older state when the current one does not
        match it.
        """
        query = self.latest_repo_rows_query()
        if query_text.strip():
            query = query.filter(IacScanRepository.project_path.ilike(f"%{query_text.strip()}%"))
        if iac == "yes":
            query = query.filter(IacScanRepository.has_iac.is_(True))
        elif iac == "no":
            query = query.filter(IacScanRepository.has_iac.is_(False))
        if scan_status:
            query = query.filter(IacScanRepository.scan_status == scan_status)
        if asoc_status:
            query = query.filter(IacScanRepository.asoc_upload_status == asoc_status)
        if has_important_findings:
            query = query.filter(
                or_(
                    IacScanRepository.critical_count > 0,
                    IacScanRepository.high_count > 0,
                )
            )
        query = query.order_by(
            IacScanRepository.critical_count.desc(),
            IacScanRepository.high_count.desc(),
            IacScanRepository.project_path.asc(),
        )
        total = query.count()
        return query.offset(offset).limit(limit).all(), total

    def repository_filter_values(self) -> dict[str, list[str]]:
        """Distinct values from the current latest state for filter menus."""
        latest = self.latest_repo_rows_query()
        scan_statuses = [
            value
            for (value,) in latest.with_entities(IacScanRepository.scan_status)
            .filter(IacScanRepository.scan_status.isnot(None))
            .distinct()
            .order_by(IacScanRepository.scan_status.asc())
            .all()
        ]
        asoc_statuses = [
            value
            for (value,) in latest.with_entities(IacScanRepository.asoc_upload_status)
            .filter(IacScanRepository.asoc_upload_status.isnot(None))
            .distinct()
            .order_by(IacScanRepository.asoc_upload_status.asc())
            .all()
        ]
        return {"scan_statuses": scan_statuses, "asoc_statuses": asoc_statuses}

    def dashboard(self) -> dict[str, Any]:
        repos = self.latest_repo_rows_query().all()
        active_repos = [r for r in repos if r.has_iac and r.scan_status == "succeeded"]
        critical = sum(int(r.critical_count or 0) for r in active_repos)
        high = sum(int(r.high_count or 0) for r in active_repos)
        findings = self._important_findings_for_latest_repos(active_repos)
        return {
            "repositories_total": len(repos),
            "iac_repositories": sum(1 for r in repos if r.has_iac),
            "scanned_repositories": len(active_repos),
            "critical": critical,
            "high": high,
            "top_problems": self._top_problem_groups(findings),
            "top_products": self._top_products(active_repos),
        }

    def run_repositories(self, run_id: int) -> list[IacScanRepository]:
        return (
            self.db.query(IacScanRepository)
            .filter(IacScanRepository.run_id == run_id)
            .order_by(
                IacScanRepository.has_iac.desc(),
                IacScanRepository.critical_count.desc(),
                IacScanRepository.high_count.desc(),
                IacScanRepository.project_path.asc(),
            )
            .all()
        )

    def get_repo_row(self, repo_row_id: int) -> IacScanRepository | None:
        return (
            self.db.query(IacScanRepository)
            .filter(IacScanRepository.id == repo_row_id)
            .first()
        )

    def repository_scan_history(
        self,
        repo_row: IacScanRepository,
        *,
        limit: int = 100,
    ) -> list[IacScanRepository]:
        """All IaC scan rows for the same GitLab repository, newest first.

        GitLab project ids are only unique inside one GitLab instance.  Join
        through the run and include ``gitlab_base_url`` in the identity so two
        instances with the same numeric project id never share a timeline.
        """
        current_run = repo_row.run or self.get_run(repo_row.run_id)
        query = (
            self.db.query(IacScanRepository)
            .join(IacScanRun, IacScanRun.id == IacScanRepository.run_id)
            .options(joinedload(IacScanRepository.run))
            .filter(IacScanRepository.gitlab_project_id == repo_row.gitlab_project_id)
        )
        if current_run and current_run.gitlab_base_url:
            query = query.filter(IacScanRun.gitlab_base_url == current_run.gitlab_base_url)
        elif repo_row.project_url:
            query = query.filter(IacScanRepository.project_url == repo_row.project_url)
        else:
            query = query.filter(IacScanRepository.project_path == repo_row.project_path)
        return (
            query.order_by(IacScanRun.created_at.desc(), IacScanRepository.id.desc())
            .limit(limit)
            .all()
        )

    def findings_for_repo_row(self, repo_row_id: int) -> list[IacScanFinding]:
        rows = (
            self.db.query(IacScanFinding)
            .filter(IacScanFinding.scan_repository_id == repo_row_id)
            .all()
        )
        return sorted(
            rows,
            key=lambda f: (
                SEVERITY_ORDER.get((f.severity or "").lower(), 99),
                f.file_path or "",
                f.line or 0,
            ),
        )

    def findings_for_problem(self, severity: str, rule_id: str, title: str) -> list[IacScanFinding]:
        latest_ids = [r.id for r in self.latest_repo_rows_query().filter(IacScanRepository.has_iac.is_(True)).all()]
        if not latest_ids:
            return []
        return (
            self.db.query(IacScanFinding)
            .filter(IacScanFinding.scan_repository_id.in_(latest_ids))
            .filter(func.lower(IacScanFinding.severity) == severity.lower())
            .filter(IacScanFinding.rule_id == rule_id)
            .filter(IacScanFinding.title == title)
            .order_by(IacScanFinding.project_path.asc(), IacScanFinding.file_path.asc())
            .all()
        )

    def latest_findings_page(
        self,
        *,
        severity: str = "",
        rule_id: str = "",
        title: str = "",
        rule_query: str = "",
        project_query: str = "",
        scanner_name: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[IacScanFinding], int]:
        """Return a deterministic page of findings from latest IaC repo rows."""
        latest_ids = (
            self.latest_repo_rows_query()
            .filter(IacScanRepository.has_iac.is_(True))
            .with_entities(IacScanRepository.id)
        )
        query = self.db.query(IacScanFinding).filter(IacScanFinding.scan_repository_id.in_(latest_ids))
        if severity:
            query = query.filter(func.lower(IacScanFinding.severity) == severity.lower())
        if rule_id:
            query = query.filter(IacScanFinding.rule_id == rule_id)
        if title:
            query = query.filter(IacScanFinding.title == title)
        if rule_query.strip():
            query = query.filter(IacScanFinding.rule_id.ilike(f"%{rule_query.strip()}%"))
        if project_query.strip():
            query = query.filter(IacScanFinding.project_path.ilike(f"%{project_query.strip()}%"))
        if scanner_name:
            query = query.filter(IacScanFinding.scanner_name == scanner_name)
        total = query.count()
        findings = (
            query.order_by(
                IacScanFinding.severity.asc(),
                IacScanFinding.rule_id.asc(),
                IacScanFinding.project_path.asc(),
                IacScanFinding.file_path.asc(),
                IacScanFinding.line.asc(),
                IacScanFinding.id.asc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return findings, total

    def _important_findings_for_latest_repos(self, repos: list[IacScanRepository]) -> list[IacScanFinding]:
        ids = [r.id for r in repos]
        if not ids:
            return []
        return (
            self.db.query(IacScanFinding)
            .filter(IacScanFinding.scan_repository_id.in_(ids))
            .filter(func.lower(IacScanFinding.severity).in_(IMPORTANT_SEVERITIES))
            .all()
        )

    @staticmethod
    def _top_problem_groups(findings: list[IacScanFinding], limit: int = 25) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for finding in findings:
            key = (
                (finding.severity or "info").lower(),
                finding.rule_id or "UNKNOWN",
                finding.title or "Untitled",
            )
            row = grouped.setdefault(
                key,
                {
                    "severity": key[0],
                    "rule_id": key[1],
                    "title": key[2],
                    "findings": 0,
                    "repositories": set(),
                    "products": set(),
                    "examples": [],
                },
            )
            row["findings"] += 1
            row["repositories"].add(finding.project_path)
            if finding.product_id:
                row["products"].add(finding.product_id)
            if finding.file_path and len(row["examples"]) < 5:
                row["examples"].append(f"{finding.project_path}:{finding.file_path}")

        out = []
        for row in grouped.values():
            out.append(
                {
                    **row,
                    "repositories_count": len(row["repositories"]),
                    "products_count": len(row["products"]),
                    "repositories": sorted(row["repositories"]),
                    "products": sorted(row["products"]),
                }
            )
        out.sort(
            key=lambda r: (
                SEVERITY_ORDER.get(r["severity"], 9),
                -int(r["repositories_count"]),
                -int(r["findings"]),
                r["rule_id"],
            )
        )
        return out[:limit]

    def _top_products(self, repos: list[IacScanRepository], limit: int = 10) -> list[dict[str, Any]]:
        product_names = {
            p.id: p.name for p in self.db.query(Product).filter(Product.id.in_([r.product_id for r in repos if r.product_id])).all()
        }
        buckets: dict[int | None, dict[str, Any]] = defaultdict(
            lambda: {"product_id": None, "product_name": "Unmapped", "repositories": 0, "critical": 0, "high": 0}
        )
        for repo in repos:
            row = buckets[repo.product_id]
            row["product_id"] = repo.product_id
            row["product_name"] = product_names.get(repo.product_id, "Unmapped")
            row["repositories"] += 1
            row["critical"] += int(repo.critical_count or 0)
            row["high"] += int(repo.high_count or 0)
        out = list(buckets.values())
        out.sort(key=lambda r: (-int(r["critical"]), -int(r["high"]), -int(r["repositories"]), r["product_name"]))
        return out[:limit]


def severity_summary(findings: list[dict[str, Any]]) -> dict[str, int]:
    out = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        sev = str(finding.get("severity") or "info").lower()
        if sev not in out:
            sev = "info"
        out[sev] += 1
    return out


def summary_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
