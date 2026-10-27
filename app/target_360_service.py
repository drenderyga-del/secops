"""Repository-scoped Target 360 read model over shared ScanOps contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import case, func, tuple_
from sqlalchemy.orm import Session, joinedload

from .control_coverage import coverage_by_repository
from .database import DatabaseService
from .execution_service import ExecutionService
from .models import (
    ActivePipeline,
    Artifact,
    Delivery,
    Execution,
    JobReport,
    Repository,
    RepositoryControlBinding,
    SecurityControl,
)
from .url_safety import safe_evidence_http_url


SOURCE_LABELS = {
    "gitlab_pipeline": "GitLab pipeline",
    "config_scan": "Config Guard",
    "iac_repository": "IaC",
    "iac_run": "IaC group",
    "ptai_asoc": "PT AI / ASOC",
    "ptai_engine": "PT AI engine",
}


class Target360Service:
    def __init__(self, db: Session):
        self.db = db

    def snapshot(
        self,
        repository_id: int,
        *,
        source: str | None = None,
        limit: int = 100,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        repository = (
            self.db.query(Repository)
            .options(joinedload(Repository.product))
            .filter(Repository.id == repository_id)
            .first()
        )
        if repository is None:
            raise LookupError("Repository not found")

        product = repository.product
        gitlab_repository_url = self._gitlab_repository_url(repository, product)
        lifecycle = ExecutionService(self.db)
        all_executions = lifecycle.repository_timeline(repository_id, limit=limit)
        executions = (
            all_executions
            if not source
            else lifecycle.repository_timeline(repository_id, limit=limit, source=source)
        )
        pipeline_urls = self._pipeline_urls(
            repository, gitlab_repository_url,
            {row['correlation_key'] for row in [*executions, *all_executions]},
        )
        for execution in executions:
            execution["source_label"] = self.source_label(execution["source"])
            execution["gitlab_url"] = pipeline_urls.get(execution["correlation_key"])
        for execution in all_executions:
            execution["gitlab_url"] = pipeline_urls.get(execution["correlation_key"])

        repository_stats = DatabaseService(self.db).get_repository_stats(repository_id)
        job_health = self._job_health(
            repository_id,
            repository.path,
            pipeline_urls,
            repository_stats,
        )

        controls = self._controls(repository_id)
        coverage = coverage_by_repository(self.db, [repository_id]).get(
            repository_id,
            {"expected": 0, "configured": 0, "missing": 0, "excluded": 0},
        )
        source_counts = dict(
            self.db.query(Execution.source, func.count(Execution.id))
            .filter(Execution.repository_id == repository_id)
            .group_by(Execution.source)
            .all()
        )
        execution_count, failed_count = (
            self.db.query(
                func.count(Execution.id),
                func.sum(case((Execution.status == "failed", 1), else_=0)),
            )
            .filter(Execution.repository_id == repository_id)
            .one()
        )
        artifact_count = (
            self.db.query(func.count(Artifact.id))
            .join(Execution, Execution.id == Artifact.execution_id)
            .filter(Execution.repository_id == repository_id)
            .scalar()
        )
        confirmed_delivery_count = (
            self.db.query(func.count(Delivery.id))
            .join(Execution, Execution.id == Delivery.execution_id)
            .filter(Execution.repository_id == repository_id, Delivery.status == "confirmed")
            .scalar()
        )
        source_options = [{"value": "all", "label": "All sources", "count": len(all_executions)}]
        source_options[0]["count"] = int(execution_count or 0)
        source_options.extend(
            {
                "value": value,
                "label": self.source_label(value),
                "count": count,
            }
            for value, count in sorted(source_counts.items(), key=lambda item: self.source_label(item[0]).lower())
        )
        summary = {
            "executions": int(execution_count or 0),
            "failed_executions": int(failed_count or 0),
            "artifacts": int(artifact_count or 0),
            "confirmed_deliveries": int(confirmed_delivery_count or 0),
        }
        return {
            "repository": repository,
            "product": repository.product,
            "gitlab_repository_url": gitlab_repository_url,
            "coverage": coverage,
            "controls": controls,
            "executions": executions,
            "source_options": source_options,
            "summary": summary,
            "job_health": job_health,
            "freshness": self._freshness(all_executions, now or datetime.now(timezone.utc)),
            "versions": self._versions(all_executions),
        }

    @staticmethod
    def _gitlab_repository_url(repository: Repository, product) -> str | None:
        if not product or not product.gitlab_base_url or not repository.path:
            return None
        path = "/".join(
            quote(segment, safe="")
            for segment in repository.path.strip("/").split("/")
            if segment
        )
        return safe_evidence_http_url(
            f"{product.gitlab_base_url.rstrip('/')}/{path}"
        )

    def _pipeline_urls(
        self,
        repository: Repository,
        repository_url: str | None,
        keys: set[str],
    ) -> dict[str, str]:
        pairs = set()
        for key in keys:
            if not isinstance(key, str) or len(key) > 80:
                continue
            parts = key.split(':')
            if len(parts) != 3 or parts[0] != 'gitlab_pipeline':
                continue
            try:
                pair = (int(parts[1]), int(parts[2]))
            except ValueError:
                continue
            if all(0 < value < 2**63 for value in pair):
                pairs.add(pair)
        if not pairs:
            return {}
        rows = (
            self.db.query(ActivePipeline.project_id, ActivePipeline.pipeline_id, ActivePipeline.web_url)
            .filter(ActivePipeline.repository_id == repository.id,
                    tuple_(ActivePipeline.project_id, ActivePipeline.pipeline_id).in_(pairs))
            .all()
        )
        urls: dict[str, str] = {}
        for pipeline in rows:
            candidate = pipeline.web_url
            if not candidate and repository_url:
                candidate = f"{repository_url}/-/pipelines/{pipeline.pipeline_id}"
            safe_url = safe_evidence_http_url(candidate)
            if safe_url:
                urls[
                    f"gitlab_pipeline:{pipeline.project_id}:{pipeline.pipeline_id}"
                ] = safe_url
        return urls

    def _job_health(
        self,
        repository_id: int,
        repository_path: str,
        pipeline_urls: dict[str, str],
        repository_stats: dict[str, Any],
    ) -> dict[str, Any]:
        service = DatabaseService(self.db)
        latest = (
            service._active_failed_jobs_query()
            .filter(JobReport.repository_id == repository_id)
            .with_entities(
                JobReport.id.label('report_id'),
                func.row_number().over(
                    partition_by=(JobReport.job_name, JobReport.ref),
                    order_by=(JobReport.finished_at.desc(), JobReport.id.desc()),
                ).label('rank'),
            )
            .subquery()
        )
        failure_limit = 100
        failures = (
            self.db.query(JobReport)
            .join(latest, JobReport.id == latest.c.report_id)
            .filter(latest.c.rank == 1)
            .options(joinedload(JobReport.detail))
            .order_by(JobReport.finished_at.desc(), JobReport.id.desc())
            .limit(failure_limit + 1)
            .all()
        )
        truncated = len(failures) > failure_limit
        fallback_keys = set()
        for report in failures[:failure_limit]:
            detail = report.detail.raw_payload if report.detail else {}
            if isinstance(detail, dict):
                fallback_keys.add(f"gitlab_pipeline:{detail.get('project_id')}:{detail.get('pipeline_id')}")
        missing = fallback_keys - pipeline_urls.keys()
        if missing:
            repository = self.db.get(Repository, repository_id)
            pipeline_urls = {**pipeline_urls, **self._pipeline_urls(
                repository, self._gitlab_repository_url(repository, repository.product), missing)}
        latest_by_key: dict[tuple[str, str | None], dict[str, Any]] = {}
        for report in failures[:failure_limit]:
            key = (report.job_name, report.ref)
            if key in latest_by_key:
                continue
            detail = report.detail.raw_payload if report.detail else {}
            detail = detail if isinstance(detail, dict) else {}
            pipeline_key = None
            project_id = detail.get("project_id")
            pipeline_id = detail.get("pipeline_id")
            if project_id is not None and pipeline_id is not None:
                pipeline_key = f"gitlab_pipeline:{project_id}:{pipeline_id}"
            pipeline_url = (
                safe_evidence_http_url(report.pipeline_url)
                or safe_evidence_http_url(detail.get("pipeline_url"))
                or pipeline_urls.get(pipeline_key)
            )
            latest_by_key[key] = {
                "name": report.job_name,
                "ref": report.ref,
                "finished_at": report.finished_at.isoformat() if report.finished_at else None,
                "job_url": safe_evidence_http_url(detail.get("job_url")),
                "pipeline_url": pipeline_url,
            }
        params = urlencode({"only_failed": "true", "status": "failed", "timeframe": "all"})
        return {
            "healthy_keys": repository_stats.get("job_health_healthy", 0),
            "broken_keys": repository_stats.get("job_health_broken", 0),
            "total_keys": repository_stats.get("job_health_total", 0),
            "failures": list(latest_by_key.values()),
            "failures_truncated": truncated,
            "failures_limit": failure_limit,
            "failures_href": f"/repos/{repository_id}/jobs?{params}",
        }

    def _controls(self, repository_id: int) -> list[dict[str, Any]]:
        priority = case(
            (RepositoryControlBinding.status == "missing", 0),
            (RepositoryControlBinding.status == "unknown", 1),
            (RepositoryControlBinding.status == "configured", 2),
            else_=3,
        )
        rows = (
            self.db.query(RepositoryControlBinding)
            .options(joinedload(RepositoryControlBinding.security_control))
            .join(SecurityControl)
            .filter(RepositoryControlBinding.repository_id == repository_id)
            .filter(SecurityControl.is_active.is_(True))
            .order_by(priority, SecurityControl.name.asc(), RepositoryControlBinding.scope.asc())
            .all()
        )
        return [
            {
                "key": row.security_control.control_key,
                "name": row.security_control.name,
                "category": row.security_control.category,
                "scope": row.scope,
                "status": row.status,
                "source": row.source,
                "evidence": row.evidence,
                "reason": row.reason,
                "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def _freshness(executions: list[dict], now: datetime) -> dict[str, str | None]:
        timestamps: list[datetime] = []
        for execution in executions:
            for value in (execution["latest_evidence_at"],):
                if value:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    timestamps.append(parsed)
                    break
        if not timestamps:
            return {"state": "empty", "label": "No execution evidence", "observed_at": None}
        observed_at = max(timestamps)
        age = max(now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc), timedelta(0))
        seconds = max(int(age.total_seconds()), 0)
        if seconds < 3600:
            label = f"{max(seconds // 60, 1)} min ago"
        elif seconds < 86400:
            label = f"{seconds // 3600} h ago"
        else:
            label = f"{seconds // 86400} d ago"
        state = "fresh" if seconds <= 86400 else "stale" if seconds <= 259200 else "expired"
        return {"state": state, "label": label, "observed_at": observed_at.isoformat()}

    @classmethod
    def _versions(cls, executions: list[dict]) -> list[str]:
        versions: list[str] = []

        def add(value: str) -> None:
            if value not in versions:
                versions.append(value)

        for execution in executions:
            for event in execution["events"]:
                data = event.get("data") or {}
                version = data.get("scanner_version") or data.get("producer_version") or data.get("version")
                if version:
                    label = "Checkov" if execution["source"] == "iac_repository" else cls.source_label(execution["source"])
                    add(f"{label} · {version}")
            for artifact in execution["artifacts"]:
                if artifact.get("schema_version"):
                    label = "SARIF" if artifact.get("kind") == "sarif" else artifact.get("kind", "Artifact").upper()
                    add(f"{label} · {artifact['schema_version']}")
        return versions

    @staticmethod
    def source_label(source: str) -> str:
        return SOURCE_LABELS.get(source, source.replace("_", " ").title())
