import math

from sqlalchemy.orm import Session, aliased, joinedload
from sqlalchemy import and_, case, exists, func, not_, or_
import os
from urllib.parse import urlparse
from .product_identity import is_nested_product_namespace
from .models import (
    Product,
    Repository,
    JobReport,
    JobReportDetail,
    RepositoryStatus,
    JobStatus,
    ActivePipeline,
    PipelineStatus,
    PipelineJob,
    PtaiScan,
    Execution,
    PTAI_TERMINAL_STATUSES,
)
from .schemas import JobIngestRequest, PipelineRegisterRequest
from .integration_settings import runtime_setting
from .execution_contract import ExecutionStatus, LegacySource
from .execution_service import ExecutionService
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Iterable, Dict, Any


PTAI_ASOC_PENDING_STATUSES = ("PENDING", "QUEUED", "ENQUEUED")
PTAI_ASOC_IN_PROGRESS_STATUSES = ("IN_PROGRESS", "PROGRESS", "RUNNING", "SCAN")
PTAI_ASOC_SUCCESS_STATUSES = ("SUCCESS", "SUCCEEDED", "DONE")
PTAI_ASOC_FAILURE_STATUSES = (
    "ERROR",
    "FAILED",
    "FAILURE",
    "ABORTED",
    "CANCELED",
    "CANCELLED",
)
PTAI_ENGINE_IN_PROGRESS_STAGES = ("scan", "running", "analysis", "processing")
PTAI_ENGINE_SUCCESS_STAGES = ("done", "completed", "success", "succeeded")
PTAI_ENGINE_FAILURE_STAGES = ("failed", "aborted")


def ptai_canonical_status_predicates() -> Dict[str, Any]:
    """Build SQL predicates equivalent to ``resolve_ptai_outcome``.

    The order here is intentional: trigger/ASOC failures win first, followed
    by raw queued/running ASOC states, partial engine outcomes, success,
    engine-side progress, and finally the pending fallback for unknown data.
    Keep this shared by list filters and summary counts so they cannot drift.
    """
    asoc_status = func.upper(func.trim(func.coalesce(PtaiScan.asoc_status, "")))
    engine_stage = func.lower(func.trim(func.coalesce(PtaiScan.ptai_stage, "")))
    engine_error = or_(
        PtaiScan.ptai_has_errors.is_(True),
        and_(PtaiScan.ptai_error.isnot(None), PtaiScan.ptai_error != ""),
    )
    trigger_failure = or_(
        PtaiScan.trigger_failed.is_(True),
        asoc_status == "TRIGGER_FAILED",
    )
    asoc_failure = asoc_status.in_(PTAI_ASOC_FAILURE_STATUSES)
    engine_failure = engine_stage.in_(PTAI_ENGINE_FAILURE_STAGES)
    error = or_(trigger_failure, asoc_failure, engine_failure)

    asoc_pending = asoc_status.in_(PTAI_ASOC_PENDING_STATUSES)
    asoc_in_progress = asoc_status.in_(PTAI_ASOC_IN_PROGRESS_STATUSES)
    asoc_partial = asoc_status == "PARTIAL_SUCCESS"
    success_like = or_(
        asoc_status.in_(PTAI_ASOC_SUCCESS_STATUSES),
        engine_stage.in_(PTAI_ENGINE_SUCCESS_STAGES),
    )
    engine_in_progress = engine_stage.in_(PTAI_ENGINE_IN_PROGRESS_STAGES)

    not_raw_pending = not_(asoc_pending)
    not_raw_in_progress = not_(asoc_in_progress)
    not_raw_partial = not_(asoc_partial)
    not_success_like = not_(success_like)

    partial_success = and_(
        not_(error),
        or_(
            asoc_partial,
            and_(
                not_raw_pending,
                not_raw_in_progress,
                engine_error,
                success_like,
            ),
        ),
    )
    success = and_(
        not_(error),
        not_raw_pending,
        not_raw_in_progress,
        not_raw_partial,
        not_(engine_error),
        success_like,
    )
    in_progress = and_(
        not_(error),
        or_(
            asoc_in_progress,
            and_(
                not_raw_pending,
                not_raw_in_progress,
                not_raw_partial,
                not_success_like,
                engine_in_progress,
            ),
        ),
    )
    pending = and_(
        not_(error),
        or_(
            asoc_pending,
            and_(
                not_raw_pending,
                not_raw_in_progress,
                not_raw_partial,
                not_(and_(engine_error, success_like)),
                not_success_like,
                not_(engine_in_progress),
            ),
        ),
    )
    return {
        "ERROR": error,
        "PARTIAL_SUCCESS": partial_success,
        "SUCCESS": success,
        "IN_PROGRESS": in_progress,
        "PENDING": pending,
    }

class DatabaseService:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create_product(self, gitlab_data: dict) -> Product:
        product, _ = self.get_or_create_product_with_meta(gitlab_data)
        return product

    def get_or_create_product_with_meta(self, gitlab_data: dict) -> Tuple[Product, bool]:
        """Get or create a product based on GitLab data"""
        raw_gitlab_url = gitlab_data.get("url", "")
        gitlab_url = self._extract_gitlab_base_url(raw_gitlab_url)
        host_key = self._gitlab_host_key(gitlab_url or raw_gitlab_url)
        project_path = gitlab_data.get("project", {}).get("path", "")
        product_name, product_path = self._derive_product_identity(project_path)

        # Find candidates by normalized host.
        candidates = self.db.query(Product).order_by(Product.id.asc()).all()
        same_host = [
            item for item in candidates
            if self._gitlab_host_key(item.gitlab_base_url) == host_key
        ]
        same_path = [
            item for item in same_host
            if (item.path or "").strip().strip("/").lower() == product_path.lower()
        ]
        product = same_path[0] if same_path else None
        same_name_host = [
            item for item in same_host
            if (item.name or "").strip().lower() == product_name.lower()
        ]
        if not product and same_name_host:
            product = same_name_host[0]

        created = False
        if not product:
            product = Product(
                name=product_name,
                path=product_path or product_name,
                gitlab_base_url=gitlab_url,
            )
            self.db.add(product)
            created = True
        else:
            merge_candidates = [item for item in same_name_host if item.id != product.id]
            if merge_candidates:
                product = self._merge_products(product, merge_candidates)
            # Keep path normalized for correct product group links in UI.
            if product.path != product_path:
                product.path = product_path
            if product.gitlab_base_url != gitlab_url:
                product.gitlab_base_url = gitlab_url
            self.db.add(product)

        self.db.commit()
        self.db.refresh(product)

        return product, created

    def cleanup_duplicate_products(self) -> Dict[str, int]:
        """Merge duplicate products with same logical key (name + gitlab host)."""
        products = self.db.query(Product).order_by(Product.id.asc()).all()
        grouped: Dict[Tuple[str, str], List[Product]] = {}
        for product in products:
            key = (
                (product.name or "").strip().lower(),
                self._gitlab_host_key(product.gitlab_base_url),
            )
            grouped.setdefault(key, []).append(product)

        merged_products = 0
        affected_groups = 0
        for _, items in grouped.items():
            if len(items) <= 1:
                continue
            primary = items[0]
            duplicates = items[1:]
            primary = self._merge_products(primary, duplicates)
            merged_products += len(duplicates)
            affected_groups += 1
            self.db.add(primary)

        self.db.commit()
        return {
            "groups": affected_groups,
            "merged_products": merged_products,
        }

    def normalize_product_paths(self) -> int:
        """Normalize persisted product paths using the same parser as product identity."""
        changed = 0
        products = self.db.query(Product).order_by(Product.id.asc()).all()
        for product in products:
            source = (product.path or "").strip() or (product.name or "").strip()
            _, normalized_path = self._derive_product_identity(source)
            if normalized_path and (product.path or "") != normalized_path:
                product.path = normalized_path
                self.db.add(product)
                changed += 1
        if changed:
            self.db.commit()
        return changed

    def _merge_products(self, primary: Product, duplicates: List[Product]) -> Product:
        if not duplicates:
            return primary
        duplicate_ids = [item.id for item in duplicates if item.id != primary.id]
        if not duplicate_ids:
            return primary

        self.db.query(Repository).filter(Repository.product_id.in_(duplicate_ids)).update(
            {Repository.product_id: primary.id},
            synchronize_session=False,
        )
        self.db.query(ActivePipeline).filter(ActivePipeline.product_id.in_(duplicate_ids)).update(
            {ActivePipeline.product_id: primary.id},
            synchronize_session=False,
        )
        self.db.flush()
        self._deduplicate_repositories_for_product(primary.id)

        for duplicate in duplicates:
            if duplicate.id == primary.id:
                continue
            self.db.delete(duplicate)
        self.db.flush()
        return primary

    def _deduplicate_repositories_for_product(self, product_id: int) -> None:
        repos = (
            self.db.query(Repository)
            .filter(Repository.product_id == product_id)
            .order_by(Repository.id.asc())
            .all()
        )
        by_path: Dict[str, Repository] = {}
        for repo in repos:
            key = (repo.path or "").strip().lower()
            if key not in by_path:
                by_path[key] = repo
                continue
            primary_repo = by_path[key]
            self.db.query(JobReport).filter(JobReport.repository_id == repo.id).update(
                {JobReport.repository_id: primary_repo.id},
                synchronize_session=False,
            )
            self.db.query(ActivePipeline).filter(ActivePipeline.repository_id == repo.id).update(
                {ActivePipeline.repository_id: primary_repo.id},
                synchronize_session=False,
            )
            self.db.delete(repo)
        self.db.flush()

    def get_or_create_repository(self, product: Product, gitlab_data: dict) -> Repository:
        """Get or create a repository based on GitLab data"""
        project_path = gitlab_data.get("project", {}).get("path", "")
        repo_path = (project_path or "").strip().strip("/") or "unknown"

        # Try to find existing repository
        repository = self.db.query(Repository).filter(
            Repository.product_id == product.id,
            Repository.path == repo_path
        ).first()

        if not repository:
            repository = Repository(
                product_id=product.id,
                path=repo_path,
                overall_status=RepositoryStatus.UNKNOWN
            )
            self.db.add(repository)
            self.db.commit()
            self.db.refresh(repository)

        return repository

    def create_job_report(self, repository: Repository, request_data: JobIngestRequest) -> JobReport:
        """Create a new job report"""
        job_data = request_data.job
        gitlab_data = request_data.gitlab

        # Map GitLab job status to our enum
        gitlab_status = job_data.get("status", "unknown").lower()
        status_mapping = {
            "success": JobStatus.SUCCESS,
            "failed": JobStatus.FAILED,
            "running": JobStatus.RUNNING,
            "cancelled": JobStatus.CANCELLED,
            "skipped": JobStatus.SKIPPED
        }
        job_status = status_mapping.get(gitlab_status, JobStatus.FAILED)

        # Parse finished_at timestamp
        finished_at_str = job_data.get("finished_at")
        finished_at = datetime.fromisoformat(finished_at_str.replace("Z", "+00:00")) if finished_at_str else datetime.utcnow()
        started_at = self._parse_gitlab_datetime(job_data.get("started_at"))
        duration_seconds = self._parse_duration_seconds(
            job_data.get("duration_seconds", job_data.get("duration"))
        )
        if duration_seconds is None:
            duration_seconds = self._duration_between_seconds(started_at, finished_at)

        job_report = JobReport(
            repository_id=repository.id,
            job_name=(job_data.get("name") or "")[:100],
            job_status=job_status,
            pipeline_url=(gitlab_data.get("pipeline_url") or "")[:255] or None,
            ref=(gitlab_data.get("ref") or job_data.get("ref") or "")[:120] or None,
            duration_seconds=duration_seconds,
            finished_at=finished_at,
        )

        self.db.add(job_report)
        self.db.flush()

        detail_payload = {
            "pipeline_url": job_report.pipeline_url,
            "ref": job_report.ref,
            "ref_kind": gitlab_data.get("ref_type") or job_data.get("ref_type"),
            "duration_seconds": duration_seconds,
            "started_at": started_at.isoformat() if started_at else job_data.get("started_at"),
            "finished_at": finished_at.isoformat(),
        }
        detail_payload = {k: v for k, v in detail_payload.items() if v not in (None, "", [])}
        if detail_payload:
            self.db.add(
                JobReportDetail(
                    job_report_id=job_report.id,
                    raw_payload=detail_payload
                )
            )

        # Update repository last_report_at and overall_status
        repository.last_report_at = finished_at
        repository.overall_status = self._calculate_repository_status(repository)

        self.db.commit()
        self.db.refresh(job_report)

        return job_report

    def _calculate_repository_status(self, repository: Repository) -> RepositoryStatus:
        """Calculate overall repository status based on latest job reports"""
        latest_jobs = self.db.query(JobReport).filter(
            JobReport.repository_id == repository.id
        ).order_by(JobReport.finished_at.desc()).limit(10).all()

        if not latest_jobs:
            return RepositoryStatus.UNKNOWN

        # Calculate success rate
        successful_jobs = len([job for job in latest_jobs if job.job_status == JobStatus.SUCCESS])
        total_jobs = len(latest_jobs)
        success_rate = (successful_jobs / total_jobs) if total_jobs > 0 else 0

        # If success rate is 100%, mark as SUCCESS
        if success_rate == 1.0:
            return RepositoryStatus.SUCCESS
        # If success rate is 0%, mark as FAILED
        elif success_rate == 0.0:
            return RepositoryStatus.FAILED
        # Otherwise, mark as FAILED (mixed results)
        else:
            return RepositoryStatus.FAILED

    def get_dashboard_stats(self, timeframe: Optional[str] = None) -> dict:
        """Get dashboard statistics"""
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        total_products = self.db.query(Product).count()
        total_repositories = self.db.query(Repository).count()
        jobs_query = self.db.query(JobReport)
        success_query = self.db.query(JobReport).filter(
            JobReport.job_status == JobStatus.SUCCESS
        )
        if cutoff:
            jobs_query = jobs_query.filter(JobReport.finished_at >= cutoff)
            success_query = success_query.filter(JobReport.finished_at >= cutoff)
        total_jobs = jobs_query.count()

        successful_jobs = success_query.count()

        # Dashboard health tracks unresolved failures: if the same repo/job/ref
        # succeeded later, that older failed attempt no longer degrades health.
        failed_jobs_count = self._active_failed_jobs_query(cutoff).count()

        # Success rate is computed per distinct job key (repository, job_name,
        # ref), NOT per raw execution row — see _job_health_counts. Counting
        # distinct keys keeps the rate consistent with the product and
        # repository pages and stops repeated successes from inflating it.
        healthy_keys, broken_keys = self._job_health_counts(cutoff=cutoff)
        effective_total = healthy_keys + broken_keys
        success_rate = (healthy_keys / effective_total * 100) if effective_total > 0 else 0

        return {
            "total_products": total_products,
            "total_repositories": total_repositories,
            "total_jobs": total_jobs,
            "success_rate": round(success_rate, 2),
            "successful_jobs": successful_jobs,
            "failed_jobs_count": failed_jobs_count,
            "healthy_jobs_count": healthy_keys,
            "broken_jobs_count": broken_keys,
            "effective_jobs_count": effective_total,
        }

    @staticmethod
    def _apply_job_category_filter(query, category: Optional[str]):
        if not category or category == "all":
            return query

        category = category.lower()
        if category == "sast":
            return query.filter(JobReport.job_name.in_(["ptai_scan", "ptai_async_scan", "semgrep_scan"]))
        if category == "sca":
            return query.filter(JobReport.job_name.ilike("sca%"))
        if category == "secrets":
            return query.filter(JobReport.job_name == "gitleaks_scan")
        if category == "cs":
            return query.filter(JobReport.job_name == "trivy_scan")
        if category == "asoc":
            return query.filter(JobReport.job_name.ilike("asoc%"))
        if category == "mobsf":
            return query.filter(JobReport.job_name.ilike("mobsf_scan"))
        return query

    @staticmethod
    def _resolve_timeframe_cutoff(timeframe: Optional[str]):
        if not timeframe:
            return None

        normalized = timeframe.lower()
        now = datetime.now(timezone.utc)

        if normalized == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)

        days_map = {
            "3d": 3,
            "7d": 7,
            "30d": 30,
            "1y": 365,
        }
        days = days_map.get(normalized)
        if days:
            return now - timedelta(days=days)
        return None

    def _active_failed_jobs_query(self, timeframe_cutoff=None):
        success_alias = aliased(JobReport)
        query = (
            self.db.query(JobReport)
            .join(Repository)
            .filter(
                JobReport.job_status == JobStatus.FAILED,
                ~exists().where(
                    (success_alias.repository_id == JobReport.repository_id)
                    & (success_alias.job_name == JobReport.job_name)
                    & (
                        (success_alias.ref == JobReport.ref)
                        | (
                            success_alias.ref.is_(None)
                            & JobReport.ref.is_(None)
                        )
                    )
                    & (success_alias.job_status == JobStatus.SUCCESS)
                    & (
                        (success_alias.finished_at > JobReport.finished_at)
                        | (
                            (success_alias.finished_at == JobReport.finished_at)
                            & (success_alias.id > JobReport.id)
                        )
                    )
                )
            )
        )
        if timeframe_cutoff:
            query = query.filter(JobReport.finished_at >= timeframe_cutoff)
        return query

    def _job_health_counts(self, *, cutoff=None, repository_id=None, product_id=None):
        """Distinct job-key health: ``(healthy_keys, broken_keys)``.

        A job key is ``(repository_id, job_name, ref)``. A key is *broken* when
        it has an unresolved failure (mirrors :meth:`_active_failed_jobs_query`),
        otherwise it is *healthy* if it ever succeeded in the window. Counting
        distinct keys on both sides keeps the success rate consistent — repeated
        successful runs no longer inflate it. Shared by dashboard, product and
        repository stats so the rate means the same thing everywhere.
        """
        red_query = self._active_failed_jobs_query(cutoff)
        success_query = self.db.query(
            JobReport.repository_id, JobReport.job_name, JobReport.ref
        ).filter(JobReport.job_status == JobStatus.SUCCESS)
        if cutoff:
            success_query = success_query.filter(JobReport.finished_at >= cutoff)
        if repository_id is not None:
            red_query = red_query.filter(JobReport.repository_id == repository_id)
            success_query = success_query.filter(JobReport.repository_id == repository_id)
        elif product_id is not None:
            red_query = red_query.filter(Repository.product_id == product_id)
            success_query = success_query.join(
                Repository, JobReport.repository_id == Repository.id
            ).filter(Repository.product_id == product_id)
        red_keys = {
            (row.repository_id, row.job_name, row.ref)
            for row in red_query.with_entities(
                JobReport.repository_id, JobReport.job_name, JobReport.ref
            ).distinct()
        }
        success_keys = {tuple(row) for row in success_query.distinct()}
        healthy = len(success_keys - red_keys)
        broken = len(red_keys)
        return healthy, broken

    def _failed_jobs_query(self, timeframe_cutoff=None):
        query = self.db.query(JobReport).join(Repository).filter(
            JobReport.job_status == JobStatus.FAILED
        )
        if timeframe_cutoff:
            query = query.filter(JobReport.finished_at >= timeframe_cutoff)
        return query

    def _all_jobs_query(self, timeframe_cutoff=None):
        query = self.db.query(JobReport).join(Repository)
        if timeframe_cutoff:
            query = query.filter(JobReport.finished_at >= timeframe_cutoff)
        return query

    def get_products_with_stats(self) -> List[dict]:
        """Get all products with their statistics"""
        from collections import defaultdict

        products = self.db.query(Product).all()

        # Per-product job-key health in two queries (no per-product N+1), so the
        # success rate matches the dashboard / repository definition.
        red_by_product = defaultdict(set)
        for row in (
            self._active_failed_jobs_query()
            .with_entities(Repository.product_id, JobReport.repository_id, JobReport.job_name, JobReport.ref)
            .distinct()
        ):
            red_by_product[row[0]].add((row[1], row[2], row[3]))
        success_by_product = defaultdict(set)
        for row in (
            self.db.query(Repository.product_id, JobReport.repository_id, JobReport.job_name, JobReport.ref)
            .join(Repository, JobReport.repository_id == Repository.id)
            .filter(JobReport.job_status == JobStatus.SUCCESS)
            .distinct()
        ):
            success_by_product[row[0]].add((row[1], row[2], row[3]))

        repositories_by_product = defaultdict(list)
        for repository in self.db.query(Repository).order_by(Repository.id).all():
            repositories_by_product[repository.product_id].append(repository)

        job_counts_by_product = {
            row.product_id: {
                "total": int(row.total or 0),
                "success": int(row.success or 0),
            }
            for row in (
                self.db.query(
                    Repository.product_id.label("product_id"),
                    func.count(JobReport.id).label("total"),
                    func.coalesce(
                        func.sum(
                            case(
                                (JobReport.job_status == JobStatus.SUCCESS, 1),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("success"),
                )
                .join(JobReport, JobReport.repository_id == Repository.id)
                .group_by(Repository.product_id)
            )
        }
        failed_counts_by_product = {
            row.product_id: int(row.failed or 0)
            for row in (
                self._active_failed_jobs_query()
                .with_entities(
                    Repository.product_id.label("product_id"),
                    func.count(JobReport.id).label("failed"),
                )
                .group_by(Repository.product_id)
            )
        }

        result = []
        for product in products:
            repositories = repositories_by_product.get(product.id, [])
            total_repos = len(repositories)
            last_scan = max(
                (repository.last_report_at for repository in repositories),
                default=None,
                key=lambda value: value or datetime.min.replace(tzinfo=timezone.utc),
            )
            job_counts = job_counts_by_product.get(
                product.id, {"total": 0, "success": 0}
            )
            red = red_by_product.get(product.id, set())
            healthy_keys = len(success_by_product.get(product.id, set()) - red)
            broken_keys = len(red)
            effective_total = healthy_keys + broken_keys
            success_rate = (healthy_keys / effective_total * 100) if effective_total else 0

            result.append({
                "product": product,
                "repositories": repositories,
                "total_repositories": total_repos,
                "last_scan": last_scan,
                "job_stats": {
                    "total": job_counts["total"],
                    "success": job_counts["success"],
                    "failed": failed_counts_by_product.get(product.id, 0),
                    "success_rate": round(success_rate, 2),
                    "health_healthy": healthy_keys,
                    "health_broken": broken_keys,
                    "health_total": effective_total,
                }
            })

        return result

    def get_repository_jobs(self, repository_id: int) -> List[JobReport]:
        """Get all job reports for a repository"""
        return self.db.query(JobReport).filter(
            JobReport.repository_id == repository_id
        ).order_by(JobReport.finished_at.desc()).all()

    def get_job_filters(
        self,
        category_filter: Optional[str] = None,
        only_failed: bool = False,
        timeframe: Optional[str] = None,
    ) -> Tuple[List[str], List[str]]:
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        # The catalog is shared by the journal and the failed-jobs queue. It
        # describes known observed job types, not only rows currently visible
        # in the unresolved-failure queue.
        base_query = self._all_jobs_query(cutoff)
        repo_query = self._apply_job_category_filter(base_query, category_filter)
        job_query = repo_query

        repo_names = [
            row[0] for row in repo_query.with_entities(Repository.path)
            .distinct()
            .order_by(Repository.path)
            .all()
        ]

        job_names = [
            row[0] for row in job_query.with_entities(JobReport.job_name)
            .distinct()
            .order_by(JobReport.job_name)
            .all()
        ]

        return repo_names, job_names

    def get_repository_stats(self, repository_id: int) -> dict:
        """Get repository statistics including success rate"""
        repository = self.db.query(Repository).filter(Repository.id == repository_id).first()
        if not repository:
            return {}

        total_jobs = self.db.query(JobReport).filter(JobReport.repository_id == repository_id).count()
        successful_jobs = self.db.query(JobReport).filter(
            JobReport.repository_id == repository_id,
            JobReport.job_status == JobStatus.SUCCESS,
        ).count()
        failed_jobs = self._active_failed_jobs_query().filter(
            JobReport.repository_id == repository_id
        ).count()
        # Key-based health rate, consistent with dashboard and product pages.
        healthy_keys, broken_keys = self._job_health_counts(repository_id=repository_id)
        effective_total = healthy_keys + broken_keys
        success_rate = (healthy_keys / effective_total * 100) if effective_total > 0 else 0

        return {
            "repository": repository,
            "total_jobs": total_jobs,
            "successful_jobs": successful_jobs,
            "failed_jobs": failed_jobs,
            "success_rate": round(success_rate, 2),
            "job_health_healthy": healthy_keys,
            "job_health_broken": broken_keys,
            "job_health_total": effective_total,
        }

    def get_repository_stats_batch(
        self, repository_ids: List[int]
    ) -> Dict[int, dict]:
        """Repository health for a collection with fixed query cost.

        The result follows :meth:`get_repository_stats` semantics, including
        unresolved-failure job keys, without issuing four queries per row.
        """
        from collections import defaultdict

        normalized_ids = sorted({int(value) for value in repository_ids})
        if not normalized_ids:
            return {}

        totals = {
            repository_id: {
                "total_jobs": int(total or 0),
                "successful_jobs": int(successful or 0),
            }
            for repository_id, total, successful in (
                self.db.query(
                    JobReport.repository_id,
                    func.count(JobReport.id),
                    func.sum(
                        case(
                            (JobReport.job_status == JobStatus.SUCCESS, 1),
                            else_=0,
                        )
                    ),
                )
                .filter(JobReport.repository_id.in_(normalized_ids))
                .group_by(JobReport.repository_id)
                .all()
            )
        }

        failed_counts = {
            repository_id: int(total or 0)
            for repository_id, total in (
                self._active_failed_jobs_query()
                .filter(JobReport.repository_id.in_(normalized_ids))
                .with_entities(
                    JobReport.repository_id,
                    func.count(JobReport.id),
                )
                .group_by(JobReport.repository_id)
                .all()
            )
        }

        broken_keys: Dict[int, set] = defaultdict(set)
        for row in (
            self._active_failed_jobs_query()
            .filter(JobReport.repository_id.in_(normalized_ids))
            .with_entities(
                JobReport.repository_id,
                JobReport.job_name,
                JobReport.ref,
            )
            .distinct()
        ):
            broken_keys[row.repository_id].add((row.job_name, row.ref))

        successful_keys: Dict[int, set] = defaultdict(set)
        for repository_id, job_name, ref in (
            self.db.query(
                JobReport.repository_id,
                JobReport.job_name,
                JobReport.ref,
            )
            .filter(
                JobReport.repository_id.in_(normalized_ids),
                JobReport.job_status == JobStatus.SUCCESS,
            )
            .distinct()
            .all()
        ):
            successful_keys[repository_id].add((job_name, ref))

        result: Dict[int, dict] = {}
        for repository_id in normalized_ids:
            broken = broken_keys.get(repository_id, set())
            healthy = successful_keys.get(repository_id, set()) - broken
            effective_total = len(healthy) + len(broken)
            counts = totals.get(
                repository_id,
                {"total_jobs": 0, "successful_jobs": 0},
            )
            result[repository_id] = {
                **counts,
                "failed_jobs": failed_counts.get(repository_id, 0),
                "job_health_healthy": len(healthy),
                "job_health_broken": len(broken),
                "job_health_total": effective_total,
                "success_rate": round(
                    len(healthy) / effective_total * 100, 2
                )
                if effective_total
                else 0,
            }
        return result

    def get_job_duration_summary(
        self,
        *,
        timeframe: Optional[str] = "7d",
        category_filter: Optional[str] = None,
        product_id: Optional[int] = None,
        repository_id: Optional[int] = None,
    ) -> dict:
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        query = self.db.query(JobReport).join(Repository).filter(JobReport.duration_seconds.isnot(None))
        if cutoff:
            query = query.filter(JobReport.finished_at >= cutoff)
        if product_id is not None:
            query = query.filter(Repository.product_id == product_id)
        if repository_id is not None:
            query = query.filter(JobReport.repository_id == repository_id)
        query = self._apply_job_category_filter(query, category_filter)
        jobs = query.order_by(JobReport.finished_at.desc()).all()
        durations = [float(job.duration_seconds) for job in jobs if job.duration_seconds is not None and job.duration_seconds >= 0]
        return self._summarize_durations(durations, latest=durations[0] if durations else None)

    def get_pipeline_duration_summary(
        self,
        *,
        timeframe: Optional[str] = "7d",
        product_id: Optional[int] = None,
        repository_id: Optional[int] = None,
    ) -> dict:
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        query = self.db.query(ActivePipeline).filter(ActivePipeline.duration_seconds.isnot(None))
        if cutoff:
            query = query.filter(ActivePipeline.finished_at >= cutoff)
        if product_id is not None:
            query = query.filter(ActivePipeline.product_id == product_id)
        if repository_id is not None:
            query = query.filter(ActivePipeline.repository_id == repository_id)
        pipelines = query.order_by(ActivePipeline.finished_at.desc()).all()
        durations = [
            float(pipeline.duration_seconds)
            for pipeline in pipelines
            if pipeline.duration_seconds is not None and pipeline.duration_seconds >= 0
        ]
        return self._summarize_durations(durations, latest=durations[0] if durations else None)

    def get_pipeline_duration_summaries_by_product(
        self, *, timeframe: Optional[str] = "30d"
    ) -> Dict[int, dict]:
        """All products' pipeline-duration summaries in ONE query (vs N per-product)."""
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        query = self.db.query(
            ActivePipeline.product_id, ActivePipeline.duration_seconds
        ).filter(ActivePipeline.duration_seconds.isnot(None))
        if cutoff:
            query = query.filter(ActivePipeline.finished_at >= cutoff)
        query = query.order_by(ActivePipeline.finished_at.desc())
        by_product: Dict[int, List[float]] = {}
        for product_id, duration in query.all():
            if product_id is None or duration is None or duration < 0:
                continue
            by_product.setdefault(product_id, []).append(float(duration))
        return {
            product_id: self._summarize_durations(durations, latest=durations[0] if durations else None)
            for product_id, durations in by_product.items()
        }

    def get_job_duration_summaries_by_product(
        self, *, timeframe: Optional[str] = "30d"
    ) -> Dict[int, dict]:
        """All products' job-duration summaries in ONE query (no category filter)."""
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        query = (
            self.db.query(Repository.product_id, JobReport.duration_seconds)
            .join(Repository, JobReport.repository_id == Repository.id)
            .filter(JobReport.duration_seconds.isnot(None))
        )
        if cutoff:
            query = query.filter(JobReport.finished_at >= cutoff)
        query = query.order_by(JobReport.finished_at.desc())
        by_product: Dict[int, List[float]] = {}
        for product_id, duration in query.all():
            if product_id is None or duration is None or duration < 0:
                continue
            by_product.setdefault(product_id, []).append(float(duration))
        return {
            product_id: self._summarize_durations(durations, latest=durations[0] if durations else None)
            for product_id, durations in by_product.items()
        }

    @staticmethod
    def _summarize_durations(durations: List[float], latest: Optional[float] = None) -> dict:
        clean = sorted(value for value in durations if value is not None and value >= 0)
        if not clean:
            return {"count": 0, "avg": None, "median": None, "p95": None, "latest": latest}

        count = len(clean)
        midpoint = count // 2
        median = clean[midpoint] if count % 2 else (clean[midpoint - 1] + clean[midpoint]) / 2
        p95_index = min(count - 1, math.ceil(count * 0.95) - 1)
        return {
            "count": count,
            "avg": sum(clean) / count,
            "median": median,
            "p95": clean[p95_index],
            "latest": latest,
        }

    def get_failed_jobs(self, limit: int = 100) -> List[JobReport]:
        query = self._active_failed_jobs_query().options(
            joinedload(JobReport.repository).joinedload(Repository.product)
        )
        return query.order_by(JobReport.finished_at.desc()).limit(limit).all()

    def count_active_failed_jobs(self, timeframe: Optional[str] = None) -> int:
        """Count unresolved failed job reports for operator queue summaries."""
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        return int(self._active_failed_jobs_query(cutoff).count())

    def get_jobs_paginated(
        self,
        repo_filter: Optional[str],
        job_filter: Optional[str],
        category_filter: Optional[str],
        status_filter: Optional[str],
        page: int,
        page_size: int,
        only_failed: bool = False,
        timeframe: Optional[str] = None,
        repository_id: Optional[int] = None,
    ) -> Tuple[List[JobReport], int]:
        query = self.get_jobs_query(
            repo_filter=repo_filter,
            job_filter=job_filter,
            category_filter=category_filter,
            status_filter=status_filter,
            only_failed=only_failed,
            timeframe=timeframe,
            repository_id=repository_id,
        )

        total = query.count()

        jobs = (
            query.options(joinedload(JobReport.repository).joinedload(Repository.product))
            .order_by(JobReport.finished_at.desc())
            .offset((page - 1) * page_size) \
            .limit(page_size) \
            .all()
        )

        return jobs, total

    def get_jobs_query(
        self,
        repo_filter: Optional[str],
        job_filter: Optional[str],
        category_filter: Optional[str],
        status_filter: Optional[str],
        only_failed: bool = False,
        timeframe: Optional[str] = None,
        repository_id: Optional[int] = None,
    ):
        """Build the canonical filtered job journal query."""
        cutoff = self._resolve_timeframe_cutoff(timeframe)
        query = self._apply_job_category_filter(
            self._active_failed_jobs_query(cutoff) if only_failed else self._all_jobs_query(cutoff),
            category_filter
        )

        if repository_id is not None:
            query = query.filter(JobReport.repository_id == repository_id)
        elif repo_filter:
            query = query.filter(Repository.path.ilike(f"%{repo_filter}%"))

        if job_filter:
            query = query.filter(JobReport.job_name.ilike(f"%{job_filter}%"))

        if status_filter and status_filter.lower() in {"success", "failed", "running", "cancelled", "skipped"}:
            status_enum = self._map_gitlab_status(status_filter)
            query = query.filter(JobReport.job_status == status_enum)

        return query

    def purge_job_details_older_than(self, days: int) -> int:
        """Remove job details older than the provided retention window."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = self.db.query(JobReportDetail).filter(JobReportDetail.created_at < cutoff).delete()
        self.db.commit()
        return deleted

    # --- Pipeline tracking helpers ---
    def register_pipeline(self, payload: PipelineRegisterRequest) -> Tuple[ActivePipeline, bool]:
        project_url = str(payload.project_url) if payload.project_url else None
        pipeline_url = self._build_pipeline_web_url(project_url, payload.project_path, payload.pipeline_id)
        gitlab_stub = {
            "url": self._extract_gitlab_base_url(project_url),
            "project": {"path": payload.project_path},
        }
        product = self.get_or_create_product(gitlab_stub)
        repository = self.get_or_create_repository(product, gitlab_stub)

        pipeline = (
            self.db.query(ActivePipeline)
            .filter(
                ActivePipeline.project_id == payload.project_id,
                ActivePipeline.pipeline_id == payload.pipeline_id,
            )
            .first()
        )

        tracked_jobs = payload.tracked_jobs or None
        created_new = False
        if pipeline:
            pipeline.status = PipelineStatus.REGISTERED
            pipeline.commit_sha = payload.commit_sha
            pipeline.git_ref = payload.ref
            pipeline.tracked_jobs = tracked_jobs
            pipeline.web_url = pipeline_url
            pipeline.started_at = None
            pipeline.finished_at = None
            pipeline.duration_seconds = None
            pipeline.error_message = None
            pipeline.completed_at = None
            pipeline.stale_at = None
            pipeline.last_polled_at = None
            pipeline.updated_at = datetime.now(timezone.utc)
        else:
            pipeline = ActivePipeline(
                project_id=payload.project_id,
                pipeline_id=payload.pipeline_id,
                product_id=product.id,
                repository_id=repository.id,
                commit_sha=payload.commit_sha,
                git_ref=payload.ref,
                tracked_jobs=tracked_jobs,
                web_url=pipeline_url,
                status=PipelineStatus.REGISTERED,
            )
            self.db.add(pipeline)
            created_new = True

        self.db.flush()
        ExecutionService(self.db).observe_legacy_execution(
            source=LegacySource.GITLAB_PIPELINE,
            raw_status="registered",
            correlation_key=f"gitlab_pipeline:{pipeline.project_id}:{pipeline.pipeline_id}",
            repository_id=pipeline.repository_id,
            product_id=pipeline.product_id,
            observed_at=datetime.now(timezone.utc),
        )
        self.db.commit()
        self.db.refresh(pipeline)
        return pipeline, created_new

    def fetch_active_pipelines(self, limit: int = 20) -> List[ActivePipeline]:
        """Return pipelines that still require polling."""
        return (
            self.db.query(ActivePipeline)
            .options(joinedload(ActivePipeline.repository).joinedload(Repository.product))
            .filter(ActivePipeline.status.in_([PipelineStatus.REGISTERED, PipelineStatus.POLLING]))
            .order_by(ActivePipeline.created_at.asc())
            .limit(limit)
            .all()
        )

    def mark_pipelines_stale(self, older_than: datetime) -> int:
        """Mark old pipelines as stale."""
        updated = (
            self.db.query(ActivePipeline)
            .filter(
                ActivePipeline.status.in_([PipelineStatus.REGISTERED, PipelineStatus.POLLING]),
                ActivePipeline.created_at < older_than,
            )
            .update(
                {
                    ActivePipeline.status: PipelineStatus.STALE,
                    ActivePipeline.stale_at: datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        return updated or 0

    def update_pipeline_poll_state(
        self,
        pipeline: ActivePipeline,
        *,
        completed: bool = False,
        error: Optional[str] = None,
        observed_job_statuses: Optional[Iterable[str]] = None,
    ) -> ActivePipeline:
        pipeline.last_polled_at = datetime.now(timezone.utc)
        job_statuses = {
            str(status).strip().lower()
            for status in (observed_job_statuses or ())
        }
        failed_statuses = {"failed", "canceled", "cancelled"}
        terminal_failed = bool(error or job_statuses.intersection(failed_statuses))
        pipeline.status = (
            PipelineStatus.FAILED
            if completed and terminal_failed
            else PipelineStatus.COMPLETED
            if completed
            else PipelineStatus.POLLING
        )
        pipeline.error_message = (
            error
            or (
                "Tracked job failed or was cancelled"
                if completed and terminal_failed
                else None
            )
        )
        self._refresh_pipeline_duration(pipeline)
        if completed:
            pipeline.completed_at = datetime.now(timezone.utc)
        self.db.add(pipeline)
        lifecycle = ExecutionService(self.db)
        correlation_key = f"gitlab_pipeline:{pipeline.project_id}:{pipeline.pipeline_id}"
        if completed:
            final_status = (
                ExecutionStatus.FAILED
                if terminal_failed
                else ExecutionStatus.SUCCEEDED
            )
            lifecycle.observe_execution(
                source=LegacySource.GITLAB_PIPELINE.value,
                correlation_key=correlation_key,
                repository_id=pipeline.repository_id,
                product_id=pipeline.product_id,
                status=final_status,
                observed_at=pipeline.completed_at,
            )
        else:
            lifecycle.observe_legacy_execution(
                source=LegacySource.GITLAB_PIPELINE,
                raw_status="polling",
                correlation_key=correlation_key,
                repository_id=pipeline.repository_id,
                product_id=pipeline.product_id,
                observed_at=pipeline.last_polled_at,
            )
        self.db.commit()
        self.db.refresh(pipeline)
        return pipeline

    def record_pipeline_job(self, pipeline: ActivePipeline, job_payload: Dict) -> PipelineJob:
        gitlab_job_id = job_payload.get("id")
        if gitlab_job_id is None:
            raise ValueError("GitLab job payload missing 'id'")

        job = (
            self.db.query(PipelineJob)
            .filter(
                PipelineJob.active_pipeline_id == pipeline.id,
                PipelineJob.gitlab_job_id == gitlab_job_id,
            )
            .first()
        )

        started_at = self._parse_gitlab_datetime(job_payload.get("started_at"))
        finished_at = self._parse_gitlab_datetime(job_payload.get("finished_at"))
        duration_seconds = self._parse_duration_seconds(job_payload.get("duration"))
        if duration_seconds is None:
            duration_seconds = self._duration_between_seconds(started_at, finished_at)

        attrs = {
            "name": job_payload.get("name"),
            "stage": job_payload.get("stage"),
            "status": job_payload.get("status"),
            "ref": job_payload.get("ref"),
            "web_url": job_payload.get("web_url"),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
            "last_seen_at": datetime.now(timezone.utc),
        }

        if job:
            for key, value in attrs.items():
                setattr(job, key, value)
        else:
            job = PipelineJob(
                active_pipeline_id=pipeline.id,
                gitlab_job_id=gitlab_job_id,
                **attrs,
            )
            self.db.add(job)

        self.db.flush()
        execution = self.db.query(Execution).filter(
            Execution.source == LegacySource.GITLAB_PIPELINE.value,
            Execution.source_scope == "default",
            Execution.correlation_key == f"gitlab_pipeline:{pipeline.project_id}:{pipeline.pipeline_id}",
        ).first()
        if execution:
            ExecutionService(self.db).observe_legacy_step(
                execution,
                source=LegacySource.GITLAB_JOB,
                raw_status=job.status,
                external_id=f"job:{job.gitlab_job_id}",
                name=job.name or f"job-{job.gitlab_job_id}",
                observed_at=job.finished_at or job.started_at or datetime.now(timezone.utc),
            )
        self.db.commit()
        self.db.refresh(job)

        # Persist into JobReport once job has final status
        final_statuses = {"success", "failed", "canceled", "skipped", "manual"}
        job_status = (job.status or "").lower()
        if job_status in final_statuses and job.finished_at:
            self._upsert_job_report_from_pipeline_job(pipeline, job)

        return job

    def ptai_scan_exists(self, gitlab_job_id: int) -> bool:
        return self.db.query(
            exists().where(PtaiScan.gitlab_job_id == gitlab_job_id)
        ).scalar()

    def record_ptai_scan(
        self,
        pipeline: ActivePipeline,
        job_payload: Dict,
        parsed_ids: Dict[str, str],
    ) -> Optional[PtaiScan]:
        """Persist one async PT AI scan captured from a finished job trace.

        Idempotent on ``gitlab_job_id``: a second call for the same job is a
        no-op (returns None). When the trace carried no ASOC ids the row is
        stored as TRIGGER_FAILED so the failure is still surfaced.
        """
        gitlab_job_id = job_payload.get("id")
        if gitlab_job_id is None:
            return None
        if self.ptai_scan_exists(gitlab_job_id):
            return None

        pipeline_id = parsed_ids.get("pipeline_id")
        task_id = parsed_ids.get("task_id")
        trigger_failed = not (pipeline_id or task_id)

        scan = PtaiScan(
            gitlab_project_id=pipeline.project_id,
            gitlab_pipeline_id=pipeline.pipeline_id,
            gitlab_job_id=gitlab_job_id,
            repository_id=pipeline.repository_id,
            product_id=pipeline.product_id,
            asoc_scan_id=parsed_ids.get("scan_id"),
            asoc_repository_id=parsed_ids.get("repository_id"),
            asoc_task_id=task_id,
            asoc_pipeline_id=pipeline_id,
            repo_slug=parsed_ids.get("repository"),
            branch=job_payload.get("ref"),
            asoc_status="TRIGGER_FAILED" if trigger_failed else "PENDING",
            trigger_failed=trigger_failed,
            job_web_url=job_payload.get("web_url"),
            error_text=None if not trigger_failed else "No ASOC ids found in job trace",
        )
        self.db.add(scan)
        try:
            self.db.flush()
            ExecutionService(self.db).observe_legacy_execution(
                source=LegacySource.PTAI_ASOC,
                raw_status=scan.asoc_status,
                correlation_key=f"ptai_asoc:{scan.gitlab_job_id}",
                repository_id=scan.repository_id,
                product_id=scan.product_id,
                observed_at=datetime.now(timezone.utc),
            )
            self.db.commit()
        except IntegrityError:
            # exists-check -> insert race (two pollers / a re-poll of the same
            # pipeline): the unique gitlab_job_id constraint fired. Roll back so
            # the session stays usable for the rest of the poll cycle.
            self.db.rollback()
            return None
        self.db.refresh(scan)
        return scan

    def list_ptai_scans(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        problem_only: bool = False,
    ) -> Tuple[List[PtaiScan], int]:
        """Newest-first page of async PT AI scans for the UI, with total count."""
        base = self.db.query(PtaiScan)
        status = (status or "").strip().upper()
        if status and status != "ALL":
            predicates = ptai_canonical_status_predicates()
            if status in predicates:
                base = base.filter(predicates[status])
            else:
                base = base.filter(PtaiScan.asoc_status == status)
        repo = (repo or "").strip()
        if repo:
            like = f"%{repo.lower()}%"
            base = base.outerjoin(Repository, PtaiScan.repository_id == Repository.id).filter(
                or_(
                    func.lower(PtaiScan.repo_slug).like(like),
                    func.lower(Repository.path).like(like),
                )
            )
        branch = (branch or "").strip()
        if branch:
            base = base.filter(func.lower(PtaiScan.branch).like(f"%{branch.lower()}%"))
        if problem_only:
            predicates = ptai_canonical_status_predicates()
            base = base.filter(or_(predicates["ERROR"], predicates["PARTIAL_SUCCESS"]))
        total = base.count()
        rows = (
            base.options(
                joinedload(PtaiScan.repository),
                joinedload(PtaiScan.product),
            )
            .order_by(PtaiScan.created_at.desc(), PtaiScan.id.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        return rows, total

    def fetch_pending_ptai_scans(self, limit: int = 50) -> List[PtaiScan]:
        """Non-terminal scans that still need an ASOC status poll.

        Two guards against starvation of fresh scans by permanently
        non-terminal rows (missing ASOC ids, ASOC that never answers):

        - an age cutoff (``PTAI_SCAN_MAX_POLL_AGE_DAYS``, default 14): rows
          older than that stop being polled at all — with the old
          ``created_at ASC`` ordering, 50+ such rows would fill every batch
          forever and new scans would never be polled;
        - fair ordering by ``last_polled_at`` (never-polled first, then the
          longest-unpolled), so within the window every row gets its turn.
        """
        max_age_days = int(os.getenv("PTAI_SCAN_MAX_POLL_AGE_DAYS", "14"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        return (
            self.db.query(PtaiScan)
            .filter(PtaiScan.trigger_failed.is_(False))
            .filter(PtaiScan.asoc_status.notin_(tuple(PTAI_TERMINAL_STATUSES)))
            .filter(PtaiScan.created_at >= cutoff)
            .order_by(PtaiScan.last_polled_at.asc().nulls_first(), PtaiScan.created_at.asc())
            .limit(limit)
            .all()
        )

    def _upsert_job_report_from_pipeline_job(self, pipeline: ActivePipeline, job: PipelineJob) -> JobReport:
        pipeline_url = (
            pipeline.web_url
            or self._pipeline_url_from_job_url(job.web_url, pipeline.pipeline_id)
            or self._build_pipeline_web_url(None, "", pipeline.pipeline_id)
        )
        report = (
            self.db.query(JobReport)
            .filter(
                JobReport.repository_id == pipeline.repository_id,
                JobReport.job_name == (job.name or "security_job")[:100],
                JobReport.pipeline_url == pipeline_url,
            )
            .first()
        )
        status = self._map_gitlab_status(job.status)
        finished_at = job.finished_at or datetime.now(timezone.utc)

        if report:
            report.job_status = status
            report.finished_at = finished_at
            report.ref = job.ref
            report.duration_seconds = job.duration_seconds
        else:
            report = JobReport(
                repository_id=pipeline.repository_id,
                job_name=(job.name or "security_job")[:100],
                job_status=status,
                pipeline_url=pipeline_url,
                ref=(job.ref or pipeline.git_ref)[:120] if job.ref or pipeline.git_ref else None,
                duration_seconds=job.duration_seconds,
                finished_at=finished_at,
            )
            self.db.add(report)

        self.db.flush()
        detail_payload = {
            "pipeline_id": pipeline.pipeline_id,
            "project_id": pipeline.project_id,
            "gitlab_job_id": job.gitlab_job_id,
            "status": job.status,
            "pipeline_url": pipeline_url,
            "job_url": job.web_url,
            "stage": job.stage,
            "duration_seconds": job.duration_seconds,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }

        detail = (
            self.db.query(JobReportDetail)
            .filter(JobReportDetail.job_report_id == report.id)
            .first()
        )
        if detail:
            detail.raw_payload = detail_payload
        else:
            self.db.add(
                JobReportDetail(
                    job_report_id=report.id,
                    raw_payload=detail_payload,
                )
            )

        repository = self.db.query(Repository).filter(Repository.id == pipeline.repository_id).first()
        if repository:
            latest = finished_at
            if repository.last_report_at:
                latest = max(repository.last_report_at, finished_at)
            repository.last_report_at = latest
            repository.overall_status = self._calculate_repository_status(repository)
            self.db.add(repository)

        self.db.commit()
        self.db.refresh(report)
        return report

    def _refresh_pipeline_duration(self, pipeline: ActivePipeline) -> None:
        jobs = (
            self.db.query(PipelineJob)
            .filter(PipelineJob.active_pipeline_id == pipeline.id)
            .all()
        )
        started_values = [job.started_at for job in jobs if job.started_at]
        finished_values = [job.finished_at for job in jobs if job.finished_at]
        if started_values:
            pipeline.started_at = min(started_values)
        if finished_values:
            pipeline.finished_at = max(finished_values)
        if pipeline.started_at and pipeline.finished_at:
            pipeline.duration_seconds = self._duration_between_seconds(pipeline.started_at, pipeline.finished_at)

    @staticmethod
    def _parse_duration_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _duration_between_seconds(started_at: Optional[datetime], finished_at: Optional[datetime]) -> Optional[float]:
        if not started_at or not finished_at:
            return None
        start = DatabaseService._as_utc(started_at)
        finish = DatabaseService._as_utc(finished_at)
        seconds = (finish - start).total_seconds()
        return seconds if seconds >= 0 else None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _build_pipeline_web_url(project_url: Optional[str], project_path: str, pipeline_id: int) -> Optional[str]:
        base = (project_url or "").strip().rstrip("/")
        if not base:
            gitlab_base = DatabaseService._extract_gitlab_base_url(None).rstrip("/")
            project = (project_path or "").strip().strip("/")
            base = f"{gitlab_base}/{project}" if gitlab_base and project else ""
        return f"{base}/-/pipelines/{pipeline_id}" if base else None

    @staticmethod
    def _pipeline_url_from_job_url(job_url: Optional[str], pipeline_id: int) -> Optional[str]:
        raw = (job_url or "").strip()
        marker = "/-/jobs/"
        if not raw or marker not in raw:
            return None
        repo_url = raw.split(marker, 1)[0].rstrip("/")
        return f"{repo_url}/-/pipelines/{pipeline_id}" if repo_url else None

    @staticmethod
    def _parse_gitlab_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _map_gitlab_status(status: Optional[str]) -> JobStatus:
        mapping = {
            "success": JobStatus.SUCCESS,
            "failed": JobStatus.FAILED,
            "running": JobStatus.RUNNING,
            "canceled": JobStatus.CANCELLED,
            "cancelled": JobStatus.CANCELLED,
            "skipped": JobStatus.SKIPPED,
        }
        return mapping.get((status or "").lower(), JobStatus.FAILED)

    @staticmethod
    def _extract_gitlab_base_url(project_url: Optional[str]) -> str:
        if project_url:
            try:
                parsed = urlparse(project_url)
                if parsed.scheme and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}"
            except ValueError:
                pass
        default_api = runtime_setting("GITLAB_API_URL", "")
        parsed_default = urlparse(default_api)
        if parsed_default.scheme and parsed_default.netloc:
            return f"{parsed_default.scheme}://{parsed_default.netloc}"
        return default_api or "https://gitlab.com"

    @staticmethod
    def _gitlab_host_key(url_or_host: Optional[str]) -> str:
        raw = (url_or_host or "").strip()
        if not raw:
            return ""
        probe = raw if "://" in raw else f"//{raw}"
        parsed = urlparse(probe)
        host = (parsed.netloc or parsed.path or "").strip().strip("/")
        if "/" in host:
            host = host.split("/", 1)[0]
        return host.lower()

    @staticmethod
    def _derive_product_identity(project_path: Optional[str]) -> Tuple[str, str]:
        raw = (project_path or "").strip().strip("/")
        if not raw:
            return "unknown", "unknown"
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            raw = (parsed.path or "").strip("/")
            if not raw:
                return "unknown", "unknown"

        segments = [segment for segment in raw.split('/') if segment]
        if not segments:
            return "unknown", "unknown"

        # Namespace depth is explicit deployment configuration. Unconfigured
        # namespaces use their first segment as the product identity.
        if is_nested_product_namespace(segments[0]) and len(segments) >= 2:
            name = segments[1]
            return name, f"{segments[0]}/{name}"

        name = segments[0]
        return name, name
