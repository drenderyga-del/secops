"""Repository-scoped Target 360 read model over shared ScanOps contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from .control_coverage import coverage_by_repository
from .execution_service import ExecutionService
from .models import Artifact, Delivery, Execution, Repository, RepositoryControlBinding, SecurityControl


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

        lifecycle = ExecutionService(self.db)
        all_executions = lifecycle.repository_timeline(repository_id, limit=limit)
        executions = (
            all_executions
            if not source
            else lifecycle.repository_timeline(repository_id, limit=limit, source=source)
        )
        for execution in executions:
            execution["source_label"] = self.source_label(execution["source"])

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
            "coverage": coverage,
            "controls": controls,
            "executions": executions,
            "source_options": source_options,
            "summary": summary,
            "freshness": self._freshness(all_executions, now or datetime.now(timezone.utc)),
            "versions": self._versions(all_executions),
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
