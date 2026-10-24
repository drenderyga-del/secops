import json
import os
import re
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .product_identity import is_nested_product_namespace
from .models import AuditCoverageStatus, AuditRepository, AuditSnapshot, JobReport, Product, Repository
from .integration_settings import runtime_setting

logger = logging.getLogger(__name__)


@dataclass
class GitLabInstanceConfig:
    host: str
    api_url: str
    token: str


def path_within(path: str, group: str) -> bool:
    """True when a lower-cased repository path lies inside a group path."""
    return bool(group) and (path == group or path.startswith(group + "/"))


class RepositoryAttribution:
    """Decide which ScanOps product a GitLab project belongs to.

    A project known to ScanOps keeps its product. Otherwise it belongs to the
    product whose own GitLab group contains it (longest group path wins). The
    audit's coarse scope — the top-level namespace, or a configured nested product namespace — is
    only a fallback for projects outside every product group, so sibling
    groups under one namespace never absorb each other's projects.
    """

    def __init__(self, db: Session):
        # Stable tie-breaks: the same group path or namespace may appear on
        # several products; the first by name then id wins, as Coverage did.
        products = db.query(Product).order_by(Product.name.asc(), Product.id.asc()).all()
        self._by_repository: Dict[Tuple[str, str], Product] = {}
        for repository, product in (
            db.query(Repository, Product).join(Product, Product.id == Repository.product_id).all()
        ):
            host = AuditService._extract_host(product.gitlab_base_url).lower().strip("/")
            path = (repository.path or "").strip("/").lower()
            if host and path:
                self._by_repository[(host, path)] = product
        self._groups: Dict[str, List[Tuple[str, Product]]] = {}
        self._scopes: Dict[str, List[Tuple[str, Product]]] = {}
        for product in products:
            host = AuditService._extract_host(product.gitlab_base_url).lower()
            if not host:
                continue
            group_path = AuditService.product_group_path(product.path)
            if group_path:
                self._groups.setdefault(host, []).append((group_path, product))
            if (product.path or "").strip():
                scope = AuditService._normalize_scope_from_product_path(product.path, product.name)
            else:
                # Without a group path Coverage has always scoped by the full name.
                scope = (product.name or "").strip().strip("/")
            if scope:
                self._scopes.setdefault(host, []).append((scope.lower(), product))
        for index in (self._groups, self._scopes):
            for candidates in index.values():
                candidates.sort(key=lambda item: len(item[0]), reverse=True)

    def product_for(self, host: Optional[str], path: Optional[str]) -> Optional[Product]:
        host_key = (host or "").lower()
        path_key = (path or "").strip("/").lower()
        product = self._by_repository.get((host_key, path_key))
        if product is not None:
            return product
        for index in (self._groups, self._scopes):
            for prefix, candidate in index.get(host_key, []):
                if path_within(path_key, prefix):
                    return candidate
        return None


class AuditService:
    def __init__(self, db: Session):
        self.db = db
        self.window_days = int(os.getenv("AUDIT_WINDOW_DAYS", "30"))
        self.max_pipelines_per_repo = int(os.getenv("AUDIT_MAX_PIPELINES_PER_REPO", "50"))
        # Per-pipeline job cap (incl. downstream). The old behaviour — one
        # global 200-job budget with a `break` — meant a busy repo burned the
        # whole budget on its first few pipelines (build/test/MR jobs) and the
        # walk never reached the pipeline that actually ran security jobs, so
        # covered repos were reported as PIPELINE_NO_SEC and alerted on.
        self.max_jobs_per_pipeline = int(os.getenv("AUDIT_MAX_JOBS_PER_PIPELINE", "200"))
        # Safety cap across the whole repo walk. Exhausting it stops fetching
        # but is reported in `reason` instead of silently skewing the verdict.
        self.max_jobs_per_repo = int(os.getenv("AUDIT_MAX_JOBS_PER_REPO", "2000"))
        self.concurrency = int(os.getenv("AUDIT_CONCURRENCY", "20"))
        self.http_timeout_seconds = float(os.getenv("AUDIT_HTTP_TIMEOUT_SECONDS", "30"))
        self.trace_gitlab = self._read_bool_env("AUDIT_TRACE_GITLAB", default=False)
        self.security_job_names = self._load_security_job_names()
        self.exclude_regex = re.compile(
            os.getenv(
                "AUDIT_EXCLUDE_REGEX",
                r"(^|/)(test|tests|qa|sandbox|playground|demo|example|docs|config|configs)(-|$|/)",
            ),
            flags=re.IGNORECASE,
        )
        self.group_scopes = self._load_group_scopes()

    async def run_scan(
        self,
        product_ids: Optional[List[int]] = None,
        progress_callback: Optional[Callable[[int, int, Optional[str]], Awaitable[None]]] = None,
    ) -> Dict[str, int]:
        instances = self._load_instance_configs()
        if not instances:
            raise RuntimeError("Audit scan is not configured: connect GitLab in Integrations")
        logger.info("Audit scan started: instances=%s selected_product_ids=%s", len(instances), product_ids or "all")
        if self.trace_gitlab:
            logger.info("Audit scan GitLab trace is enabled (AUDIT_TRACE_GITLAB=true)")
        target_product_ids = product_ids or [row[0] for row in self.db.query(Product.id).all()]
        if not target_product_ids:
            logger.info("Audit scan finished: no products in monitoring")
            return {
                "total": 0,
                "covered": 0,
                "pipeline_no_sec": 0,
                "no_pipelines": 0,
                "inaccessible": 0,
                "excluded": 0,
            }
        target_products = self.db.query(Product).filter(Product.id.in_(target_product_ids)).all()
        scoped_targets = self._build_scoped_targets(instances, target_products)
        if not scoped_targets:
            logger.warning("Audit scan finished: no scoped targets resolved from selected products")
            return {
                "total": 0,
                "covered": 0,
                "pipeline_no_sec": 0,
                "no_pipelines": 0,
                "inaccessible": 0,
                "excluded": 0,
            }

        scanned_at = datetime.now(timezone.utc)
        inventory: List[Tuple[GitLabInstanceConfig, Dict[str, Any]]] = []

        timeout = httpx.Timeout(self.http_timeout_seconds)

        async with httpx.AsyncClient(timeout=timeout) as client:
            if progress_callback:
                await progress_callback(0, 0, "Collecting repositories from GitLab...")
            for instance, scope, product_name in scoped_targets:
                if progress_callback:
                    await progress_callback(0, 0, f"Collecting projects from {instance.host}/{scope}")
                logger.info(
                    "Audit scan: collecting projects for product=%s from %s/%s",
                    product_name,
                    instance.host,
                    scope,
                )
                projects = []
                resolved_scope = None
                scope_candidates = self._scope_resolution_candidates(scope, product_name)
                for candidate in scope_candidates:
                    try:
                        projects = await self._fetch_projects_for_group(client, instance, candidate)
                        resolved_scope = candidate
                        break
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code if exc.response else None
                        if status == 404:
                            logger.warning(
                                "Audit scan: scope not found for product=%s host=%s scope=%s, trying fallback",
                                product_name,
                                instance.host,
                                candidate,
                            )
                            continue
                        logger.exception(
                            "Audit scan: failed to collect projects for product=%s from %s/%s",
                            product_name,
                            instance.host,
                            candidate,
                        )
                        projects = []
                        resolved_scope = None
                        break
                    except Exception:
                        logger.exception(
                            "Audit scan: failed to collect projects for product=%s from %s/%s",
                            product_name,
                            instance.host,
                            candidate,
                        )
                        projects = []
                        resolved_scope = None
                        break

                if not resolved_scope:
                    logger.error(
                        "Audit scan: skip product=%s host=%s, no valid group scope resolved from %s",
                        product_name,
                        instance.host,
                        scope,
                    )
                    continue
                logger.info(
                    "Audit scan: %s projects fetched for product=%s from %s/%s",
                    len(projects),
                    product_name,
                    instance.host,
                    resolved_scope,
                )
                for project in projects:
                    inventory.append((instance, project))

            dedup_inventory: Dict[Tuple[str, int], Tuple[GitLabInstanceConfig, Dict[str, Any]]] = {}
            for instance, project in inventory:
                key = (instance.host.lower(), int(project.get("id") or 0))
                if key[1] == 0:
                    continue
                dedup_inventory[key] = (instance, project)
            inventory = list(dedup_inventory.values())

            limits = httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency)
            async with httpx.AsyncClient(timeout=timeout, limits=limits) as worker_client:
                tasks = [self._scan_project(worker_client, instance, project) for instance, project in inventory]
                project_results = await self._run_bounded(tasks, self.concurrency)
                if progress_callback:
                    await progress_callback(0, len(project_results), "Starting repository scan...")

        logger.info("Audit scan: repositories matched=%s", len(project_results))

        stats = {
            "total": 0,
            "covered": 0,
            "pipeline_no_sec": 0,
            "no_pipelines": 0,
            "inaccessible": 0,
            "excluded": 0,
        }

        for index, (instance, project, result) in enumerate(project_results, start=1):
            repo_row = self._upsert_repository(instance, project)
            snapshot = AuditSnapshot(
                audit_repository_id=repo_row.id,
                scanned_at=scanned_at,
                window_days=self.window_days,
                status=result["status"],
                reason=result.get("reason"),
                pipeline_count=result.get("pipeline_count", 0),
                security_job_count=result.get("security_job_count", 0),
                last_pipeline_at=result.get("last_pipeline_at"),
                error_message=result.get("error_message"),
            )
            self.db.add(snapshot)

            stats["total"] += 1
            stats[result["status"].value] += 1
            if progress_callback:
                await progress_callback(index, len(project_results), project.get("path_with_namespace"))

        self.db.commit()
        logger.info(
            "Audit scan completed: total=%s covered=%s not_configured=%s no_pipelines=%s inaccessible=%s excluded=%s",
            stats["total"],
            stats["covered"],
            stats["pipeline_no_sec"],
            stats["no_pipelines"],
            stats["inaccessible"],
            stats["excluded"],
        )
        return stats

    def get_latest_product_coverage(self) -> Dict[int, Dict[str, Any]]:
        latest_scan = self.db.query(func.max(AuditSnapshot.scanned_at)).scalar()
        if not latest_scan:
            return {}

        coverage: Dict[int, Dict[str, Any]] = {}
        for product_id, rows in self.latest_repositories_by_product().items():
            coverage[product_id] = {
                "covered": 0,
                "total": 0,
                "last_scanned_at": latest_scan,
            }
            for snapshot, _repo in rows:
                if snapshot.status != AuditCoverageStatus.EXCLUDED:
                    coverage[product_id]["total"] += 1
                    if snapshot.status == AuditCoverageStatus.COVERED:
                        coverage[product_id]["covered"] += 1

        return coverage

    def latest_repositories_by_product(
        self,
    ) -> Dict[int, List[Tuple[AuditSnapshot, AuditRepository]]]:
        """Latest audit observation of every GitLab repository, per product group.

        The audit enumerates every project inside a product's GitLab group, so
        the result also contains repositories ScanOps has never received a
        security job from.
        """
        attribution = RepositoryAttribution(self.db)
        by_product: Dict[int, List[Tuple[AuditSnapshot, AuditRepository]]] = {}
        for snapshot, repo in self._latest_snapshot_rows().all():
            product = attribution.product_for(repo.instance_host, repo.path_with_namespace)
            if product is not None:
                by_product.setdefault(product.id, []).append((snapshot, repo))

        return by_product

    def get_latest_summary(self) -> Dict[str, Any]:
        latest_scan = self.db.query(func.max(AuditSnapshot.scanned_at)).scalar()
        if not latest_scan:
            return {
                "last_scanned_at": None,
                "total": 0,
                "covered": 0,
                "pipeline_no_sec": 0,
                "no_pipelines": 0,
                "inaccessible": 0,
                "excluded": 0,
            }

        latest_per_repo = self._latest_snapshot_subquery()
        rows = (
            self.db.query(AuditSnapshot.status, func.count(AuditSnapshot.id))
            .join(
                latest_per_repo,
                and_(
                    AuditSnapshot.audit_repository_id == latest_per_repo.c.repo_id,
                    AuditSnapshot.scanned_at == latest_per_repo.c.latest_scanned_at,
                ),
            )
            .group_by(AuditSnapshot.status)
            .all()
        )
        counts = {status: count for status, count in rows}
        return {
            "last_scanned_at": latest_scan,
            "total": sum(counts.values()),
            "covered": counts.get(AuditCoverageStatus.COVERED, 0),
            "pipeline_no_sec": counts.get(AuditCoverageStatus.PIPELINE_NO_SEC, 0),
            "no_pipelines": counts.get(AuditCoverageStatus.NO_PIPELINES, 0),
            "inaccessible": counts.get(AuditCoverageStatus.INACCESSIBLE, 0),
            "excluded": counts.get(AuditCoverageStatus.EXCLUDED, 0),
        }

    def get_latest_lifecycle_summary(self, *, stop_window_days: int = 14) -> Dict[str, int]:
        latest_scan = self.db.query(func.max(AuditSnapshot.scanned_at)).scalar()
        if not latest_scan:
            return {
                "active_recent": 0,
                "stopped_recently": 0,
                "never_seen": 0,
                "inactive_long": 0,
            }

        latest_per_repo = self._latest_snapshot_subquery()
        rows = (
            self.db.query(AuditSnapshot.audit_repository_id, AuditSnapshot.status)
            .join(
                latest_per_repo,
                and_(
                    AuditSnapshot.audit_repository_id == latest_per_repo.c.repo_id,
                    AuditSnapshot.scanned_at == latest_per_repo.c.latest_scanned_at,
                ),
            )
            .all()
        )
        repo_ids = [repo_id for repo_id, _ in rows]
        if not repo_ids:
            return {
                "active_recent": 0,
                "stopped_recently": 0,
                "never_seen": 0,
                "inactive_long": 0,
            }

        ever_positive = {
            repo_id
            for (repo_id,) in (
                self.db.query(AuditSnapshot.audit_repository_id)
                .filter(
                    AuditSnapshot.audit_repository_id.in_(repo_ids),
                    AuditSnapshot.security_job_count > 0,
                )
                .distinct()
                .all()
            )
        }
        cutoff = latest_scan - timedelta(days=stop_window_days)
        recent_positive = {
            repo_id
            for (repo_id,) in (
                self.db.query(AuditSnapshot.audit_repository_id)
                .filter(
                    AuditSnapshot.audit_repository_id.in_(repo_ids),
                    AuditSnapshot.security_job_count > 0,
                    AuditSnapshot.scanned_at >= cutoff,
                )
                .distinct()
                .all()
            )
        }

        summary = {
            "active_recent": 0,
            "stopped_recently": 0,
            "never_seen": 0,
            "inactive_long": 0,
        }

        for repo_id, status in rows:
            if status == AuditCoverageStatus.COVERED:
                summary["active_recent"] += 1
                continue
            if status not in (AuditCoverageStatus.PIPELINE_NO_SEC, AuditCoverageStatus.NO_PIPELINES):
                continue
            if repo_id in recent_positive:
                summary["stopped_recently"] += 1
            elif repo_id in ever_positive:
                summary["inactive_long"] += 1
            else:
                summary["never_seen"] += 1

        return summary

    def get_scan_trend(self, *, limit: int = 10) -> List[Dict[str, Any]]:
        scans = (
            self.db.query(AuditSnapshot.scanned_at)
            .distinct()
            .order_by(AuditSnapshot.scanned_at.desc())
            .limit(limit)
            .all()
        )
        scan_points = [item[0] for item in scans][::-1]
        if not scan_points:
            return []

        rows = (
            self.db.query(
                AuditSnapshot.scanned_at,
                AuditSnapshot.status,
                func.count(AuditSnapshot.id),
            )
            .filter(AuditSnapshot.scanned_at.in_(scan_points))
            .group_by(AuditSnapshot.scanned_at, AuditSnapshot.status)
            .all()
        )

        buckets: Dict[datetime, Dict[str, int]] = {}
        for scanned_at, status, count in rows:
            if scanned_at not in buckets:
                buckets[scanned_at] = {
                    "covered": 0,
                    "pipeline_no_sec": 0,
                    "no_pipelines": 0,
                    "inaccessible": 0,
                    "excluded": 0,
                }
            buckets[scanned_at][status.value] = int(count)

        result: List[Dict[str, Any]] = []
        for point in scan_points:
            data = buckets.get(
                point,
                {
                    "covered": 0,
                    "pipeline_no_sec": 0,
                    "no_pipelines": 0,
                    "inaccessible": 0,
                    "excluded": 0,
                },
            )
            total = sum(data.values())
            result.append(
                {
                    "scanned_at": point,
                    "total": total,
                    "covered": data["covered"],
                    "problem": data["pipeline_no_sec"] + data["no_pipelines"] + data["inaccessible"],
                }
            )
        return result

    def get_latest_rows(self, *, only_problems: bool = False, stop_window_days: int = 14) -> List[Dict[str, Any]]:
        latest_scan = self.db.query(func.max(AuditSnapshot.scanned_at)).scalar()
        if not latest_scan:
            return []

        query = self._latest_snapshot_rows().order_by(AuditRepository.path_with_namespace.asc())
        if only_problems:
            query = query.filter(
                AuditSnapshot.status.in_(
                    [
                        AuditCoverageStatus.PIPELINE_NO_SEC,
                        AuditCoverageStatus.NO_PIPELINES,
                        AuditCoverageStatus.INACCESSIBLE,
                    ]
                )
            )

        pairs = query.all()
        repo_ids = [repo.id for _, repo in pairs]
        cutoff = latest_scan - timedelta(days=stop_window_days)
        ever_positive = {
            repo_id
            for (repo_id,) in (
                self.db.query(AuditSnapshot.audit_repository_id)
                .filter(
                    AuditSnapshot.audit_repository_id.in_(repo_ids or [-1]),
                    AuditSnapshot.security_job_count > 0,
                )
                .distinct()
                .all()
            )
        }
        recent_positive = {
            repo_id
            for (repo_id,) in (
                self.db.query(AuditSnapshot.audit_repository_id)
                .filter(
                    AuditSnapshot.audit_repository_id.in_(repo_ids or [-1]),
                    AuditSnapshot.security_job_count > 0,
                    AuditSnapshot.scanned_at >= cutoff,
                )
                .distinct()
                .all()
            )
        }

        rows = []
        for snapshot, repo in pairs:
            lifecycle = "n/a"
            if snapshot.status == AuditCoverageStatus.COVERED:
                lifecycle = "active_recent"
            elif snapshot.status in (AuditCoverageStatus.PIPELINE_NO_SEC, AuditCoverageStatus.NO_PIPELINES):
                if repo.id in recent_positive:
                    lifecycle = "stopped_recently"
                elif repo.id in ever_positive:
                    lifecycle = "inactive_long"
                else:
                    lifecycle = "never_seen"
            rows.append(
                {
                    "snapshot": snapshot,
                    "repository": repo,
                    "lifecycle": lifecycle,
                }
            )
        return rows

    def _latest_snapshot_subquery(self):
        return (
            self.db.query(
                AuditSnapshot.audit_repository_id.label("repo_id"),
                func.max(AuditSnapshot.scanned_at).label("latest_scanned_at"),
            )
            .group_by(AuditSnapshot.audit_repository_id)
            .subquery()
        )

    def _latest_snapshot_rows(self):
        latest_per_repo = self._latest_snapshot_subquery()
        return (
            self.db.query(AuditSnapshot, AuditRepository)
            .join(AuditRepository, AuditRepository.id == AuditSnapshot.audit_repository_id)
            .join(
                latest_per_repo,
                and_(
                    AuditSnapshot.audit_repository_id == latest_per_repo.c.repo_id,
                    AuditSnapshot.scanned_at == latest_per_repo.c.latest_scanned_at,
                ),
            )
        )

    async def _scan_project(
        self,
        client: httpx.AsyncClient,
        instance: GitLabInstanceConfig,
        project: Dict[str, Any],
    ) -> Tuple[GitLabInstanceConfig, Dict[str, Any], Dict[str, Any]]:
        project_path = project.get("path_with_namespace") or f"id:{project.get('id')}"
        if self.trace_gitlab:
            logger.info("Audit scan project start: host=%s project=%s", instance.host, project_path)

        excluded, exclude_reason = self._is_excluded(project)
        if excluded:
            if self.trace_gitlab:
                logger.info(
                    "Audit scan project excluded: host=%s project=%s reason=%s",
                    instance.host,
                    project_path,
                    exclude_reason,
                )
            return (
                instance,
                project,
                {
                    "status": AuditCoverageStatus.EXCLUDED,
                    "reason": exclude_reason,
                    "pipeline_count": 0,
                    "security_job_count": 0,
                    "last_pipeline_at": None,
                },
            )

        headers = {"PRIVATE-TOKEN": instance.token}
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.window_days)

        pipelines_path = f"{instance.api_url}/projects/{project['id']}/pipelines"
        pipelines_params = {
            "per_page": min(100, self.max_pipelines_per_repo),
            "updated_after": cutoff.isoformat(),
            "order_by": "updated_at",
            "sort": "desc",
        }
        try:
            pipelines = await self._fetch_paginated(
                client,
                pipelines_path,
                headers,
                pipelines_params,
                max_items=self.max_pipelines_per_repo,
            )
        except Exception as exc:
            logger.warning(
                "Audit scan project pipelines error: host=%s project=%s error=%s",
                instance.host,
                project_path,
                exc,
            )
            return (
                instance,
                project,
                {
                    "status": AuditCoverageStatus.INACCESSIBLE,
                    "reason": "GitLab API error while fetching pipelines",
                    "error_message": str(exc),
                    "pipeline_count": 0,
                    "security_job_count": 0,
                    "last_pipeline_at": None,
                },
            )

        pipeline_count = len(pipelines)
        last_pipeline_at = self._parse_any_datetime(
            pipelines[0].get("updated_at") if pipelines else None
        )

        if pipeline_count == 0:
            if self._has_recent_security_reports(project, instance, cutoff):
                # GitLab shows no pipelines (token scope, hidden pipelines…),
                # but the secpipe reported runs to the monitor — it works.
                return (
                    instance,
                    project,
                    {
                        "status": AuditCoverageStatus.COVERED,
                        "reason": "Security jobs reported to the monitor within the audit window",
                        "pipeline_count": 0,
                        "security_job_count": 1,
                        "last_pipeline_at": None,
                    },
                )
            if self.trace_gitlab:
                logger.info("Audit scan project no pipelines: host=%s project=%s", instance.host, project_path)
            return (
                instance,
                project,
                {
                    "status": AuditCoverageStatus.NO_PIPELINES,
                    "reason": f"No pipelines in the last {self.window_days} days",
                    "pipeline_count": 0,
                    "security_job_count": 0,
                    "last_pipeline_at": None,
                },
            )

        try:
            security_job_count = 0
            scanned_jobs = 0
            budget_exhausted = False
            visited_pipelines: Set[Tuple[int, int]] = set()
            project_id = int(project["id"])
            # Walk EVERY pipeline in the window (the fetch above is already
            # capped by max_pipelines_per_repo). Jobs are counted per pipeline
            # and the walk stops as soon as a security job is found, so a repo
            # whose secpipe last ran a few pipelines ago is still COVERED and
            # covered repos cost only their first pipeline in API calls.
            for pipeline in pipelines:
                pipeline_id = int(pipeline.get("id") or 0)
                if pipeline_id <= 0:
                    continue
                if scanned_jobs >= self.max_jobs_per_repo:
                    budget_exhausted = True
                    break
                pipeline_jobs = await self._fetch_pipeline_jobs_with_downstream(
                    client=client,
                    instance=instance,
                    project_id=project_id,
                    pipeline_id=pipeline_id,
                    max_items=min(self.max_jobs_per_pipeline, self.max_jobs_per_repo - scanned_jobs),
                    visited=visited_pipelines,
                )
                scanned_jobs += len(pipeline_jobs)
                for job in pipeline_jobs:
                    name = (job.get("name") or "").strip().lower()
                    if name not in self.security_job_names:
                        continue
                    ts = self._parse_any_datetime(job.get("finished_at") or job.get("created_at"))
                    if ts and ts >= cutoff:
                        security_job_count += 1
                if security_job_count > 0:
                    break
        except Exception as exc:
            logger.warning(
                "Audit scan project jobs error: host=%s project=%s error=%s",
                instance.host,
                project_path,
                exc,
            )
            return (
                instance,
                project,
                {
                    "status": AuditCoverageStatus.INACCESSIBLE,
                    "reason": "GitLab API error while fetching jobs",
                    "error_message": str(exc),
                    "pipeline_count": pipeline_count,
                    "security_job_count": 0,
                    "last_pipeline_at": last_pipeline_at,
                },
            )

        if security_job_count > 0:
            status = AuditCoverageStatus.COVERED
            reason = "Security jobs detected in recent pipelines"
        elif self._has_recent_security_reports(project, instance, cutoff):
            # The secpipe reports every run into job_reports itself — that is a
            # direct "connected and working" signal even when the GitLab walk
            # missed the job (deep pipeline, cross-project downstream, budget).
            status = AuditCoverageStatus.COVERED
            security_job_count = 1
            reason = "Security jobs reported to the monitor within the audit window"
        else:
            status = AuditCoverageStatus.PIPELINE_NO_SEC
            reason = f"Pipelines exist, but no security jobs found in {self.window_days} days"
            if budget_exhausted:
                reason += f" (job scan budget of {self.max_jobs_per_repo} exhausted — coverage may be incomplete)"

        if self.trace_gitlab:
            logger.info(
                "Audit scan project done: host=%s project=%s status=%s pipelines=%s sec_jobs=%s",
                instance.host,
                project_path,
                status.value,
                pipeline_count,
                security_job_count,
            )

        return (
            instance,
            project,
            {
                "status": status,
                "reason": reason,
                "pipeline_count": pipeline_count,
                "security_job_count": security_job_count,
                "last_pipeline_at": last_pipeline_at,
            },
        )

    async def _fetch_projects_for_instance(
        self,
        client: httpx.AsyncClient,
        instance: GitLabInstanceConfig,
    ) -> List[Dict[str, Any]]:
        headers = {"PRIVATE-TOKEN": instance.token}
        scopes = self.group_scopes.get(instance.host, [])
        projects: List[Dict[str, Any]] = []

        if scopes:
            for scope in scopes:
                group_id = quote(scope, safe="")
                url = f"{instance.api_url}/groups/{group_id}/projects"
                params = {
                    "include_subgroups": True,
                    "archived": False,
                    "per_page": 100,
                    "order_by": "id",
                    "simple": True,
                }
                page_projects = await self._fetch_paginated(client, url, headers, params)
                projects.extend(page_projects)
        else:
            url = f"{instance.api_url}/projects"
            params = {
                "archived": False,
                "membership": False,
                "per_page": 100,
                "simple": True,
                "order_by": "id",
            }
            projects = await self._fetch_paginated(client, url, headers, params)

        dedup = {}
        for project in projects:
            key = int(project.get("id") or 0)
            if key:
                dedup[key] = project
        return list(dedup.values())

    async def _fetch_projects_for_group(
        self,
        client: httpx.AsyncClient,
        instance: GitLabInstanceConfig,
        scope: str,
    ) -> List[Dict[str, Any]]:
        headers = {"PRIVATE-TOKEN": instance.token}
        group_id = quote(scope.strip("/"), safe="")
        url = f"{instance.api_url}/groups/{group_id}/projects"
        params = {
            "include_subgroups": True,
            "archived": False,
            "per_page": 100,
            "order_by": "id",
            "simple": True,
        }
        projects = await self._fetch_paginated(client, url, headers, params)
        dedup: Dict[int, Dict[str, Any]] = {}
        for project in projects:
            key = int(project.get("id") or 0)
            if key:
                dedup[key] = project
        return list(dedup.values())

    def _upsert_repository(self, instance: GitLabInstanceConfig, project: Dict[str, Any]) -> AuditRepository:
        row = (
            self.db.query(AuditRepository)
            .filter(
                AuditRepository.instance_host == instance.host,
                AuditRepository.project_id == int(project["id"]),
            )
            .first()
        )

        excluded, exclude_reason = self._is_excluded(project)
        last_activity_at = self._parse_any_datetime(project.get("last_activity_at"))

        if row is None:
            row = AuditRepository(
                instance_host=instance.host,
                project_id=int(project["id"]),
                path_with_namespace=(project.get("path_with_namespace") or "")[:255],
                web_url=(project.get("web_url") or "")[:255] or None,
                default_branch=(project.get("default_branch") or "")[:120] or None,
                archived=bool(project.get("archived")),
                empty_repo=bool(project.get("empty_repo")),
                excluded=excluded,
                exclude_reason=(exclude_reason or "")[:255] or None,
                last_activity_at=last_activity_at,
            )
            self.db.add(row)
            self.db.flush()
            return row

        row.path_with_namespace = (project.get("path_with_namespace") or row.path_with_namespace)[:255]
        row.web_url = (project.get("web_url") or "")[:255] or row.web_url
        row.default_branch = (project.get("default_branch") or "")[:120] or None
        row.archived = bool(project.get("archived"))
        row.empty_repo = bool(project.get("empty_repo"))
        row.excluded = excluded
        row.exclude_reason = (exclude_reason or "")[:255] or None
        row.last_activity_at = last_activity_at
        self.db.add(row)
        self.db.flush()
        return row

    def _has_recent_security_reports(self, project: Dict[str, Any], instance: GitLabInstanceConfig, cutoff: datetime) -> bool:
        """True when the secpipe itself reported a security job for this repo.

        Every secpipe run POSTs a job report into ``job_reports``; a recent row
        with a security job name is direct proof of a connected pipeline,
        independent of how deep in GitLab history that pipeline is. Matched by
        repository path plus GitLab host (paths may collide across the two
        GitLab instances).
        """
        path = (project.get("path_with_namespace") or "").strip().strip("/")
        if not path:
            return False
        try:
            query = (
                self.db.query(JobReport.id)
                .join(Repository, JobReport.repository_id == Repository.id)
                .join(Product, Repository.product_id == Product.id)
                .filter(
                    func.lower(Repository.path) == path.lower(),
                    JobReport.finished_at >= cutoff,
                    func.lower(JobReport.job_name).in_(self.security_job_names),
                )
            )
            host = (instance.host or "").strip().lower()
            if host:
                query = query.filter(func.lower(Product.gitlab_base_url).contains(host))
            return self.db.query(query.exists()).scalar() or False
        except Exception:
            # The fallback must never break the scan — no report is treated
            # the same as "no evidence".
            logger.exception("Audit: job_reports fallback failed for %s", path)
            return False

    async def _fetch_pipeline_jobs_with_downstream(
        self,
        client: httpx.AsyncClient,
        instance: GitLabInstanceConfig,
        project_id: int,
        pipeline_id: int,
        max_items: int,
        visited: Optional[Set[Tuple[int, int]]] = None,
    ) -> List[Dict[str, Any]]:
        if max_items <= 0:
            return []

        if visited is None:
            visited = set()
        key = (project_id, pipeline_id)
        if key in visited:
            return []
        visited.add(key)

        headers = {"PRIVATE-TOKEN": instance.token}
        jobs_url = f"{instance.api_url}/projects/{project_id}/pipelines/{pipeline_id}/jobs"
        jobs_params = {
            "per_page": 100,
            "order_by": "updated_at",
            "sort": "desc",
        }
        jobs = await self._fetch_paginated(
            client,
            jobs_url,
            headers,
            jobs_params,
            max_items=max_items,
        )
        if len(jobs) >= max_items:
            return jobs[:max_items]

        bridges_url = f"{instance.api_url}/projects/{project_id}/pipelines/{pipeline_id}/bridges"
        bridges_params = {
            "per_page": 100,
            "order_by": "updated_at",
            "sort": "desc",
        }
        try:
            bridges = await self._fetch_paginated(
                client,
                bridges_url,
                headers,
                bridges_params,
            )
        except Exception as exc:
            logger.debug(
                "Audit scan bridges fetch skipped: host=%s project=%s pipeline=%s error=%s",
                instance.host,
                project_id,
                pipeline_id,
                exc,
            )
            return jobs

        for bridge in bridges:
            remaining = max_items - len(jobs)
            if remaining <= 0:
                break
            downstream = bridge.get("downstream_pipeline") or {}
            child_pipeline_id = int(downstream.get("id") or 0)
            if child_pipeline_id <= 0:
                continue
            child_project_id = int(downstream.get("project_id") or project_id)
            child_jobs = await self._fetch_pipeline_jobs_with_downstream(
                client=client,
                instance=instance,
                project_id=child_project_id,
                pipeline_id=child_pipeline_id,
                max_items=remaining,
                visited=visited,
            )
            jobs.extend(child_jobs)

        return jobs[:max_items]

    async def _fetch_paginated(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Dict[str, str],
        params: Dict[str, Any],
        max_items: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        page = 1
        per_page = int(params.get("per_page", 100))
        items: List[Dict[str, Any]] = []

        while True:
            query = dict(params)
            query["page"] = page
            response = await self._gitlab_get(client, url, headers=headers, params=query)
            payload = response.json()
            if not isinstance(payload, list):
                logger.warning(
                    "Audit fetch unexpected payload type: url=%s page=%s type=%s",
                    url,
                    page,
                    type(payload).__name__,
                )
                break

            items.extend(payload)
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]

            if len(payload) < per_page:
                break

            page += 1

        return items

    async def _gitlab_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: Dict[str, str],
        params: Dict[str, Any],
    ) -> httpx.Response:
        started = time.perf_counter()
        safe_headers = self._sanitize_headers_for_log(headers)
        if self.trace_gitlab:
            logger.info(
                "GitLab request: method=GET url=%s params=%s headers=%s",
                url,
                params,
                safe_headers,
            )
        try:
            response = await client.get(url, headers=headers, params=params)
        except Exception:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "GitLab request failed: method=GET url=%s params=%s duration_ms=%s",
                url,
                params,
                elapsed_ms,
            )
            raise

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if self.trace_gitlab or response.status_code >= 400:
            logger.info(
                "GitLab response: method=GET url=%s status=%s duration_ms=%s",
                url,
                response.status_code,
                elapsed_ms,
            )

        if response.status_code >= 400:
            body = (response.text or "").strip().replace("\n", " ")
            if len(body) > 800:
                body = body[:800] + "...(truncated)"
            logger.error(
                "GitLab HTTP error: method=GET url=%s status=%s duration_ms=%s body=%s",
                url,
                response.status_code,
                elapsed_ms,
                body,
            )
            response.raise_for_status()

        return response

    async def _run_bounded(self, coroutines: List[Any], limit: int) -> List[Any]:
        if not coroutines:
            return []
        semaphore = asyncio.Semaphore(limit)

        async def _wrapped(coro):
            async with semaphore:
                return await coro

        return await asyncio.gather(*[_wrapped(coro) for coro in coroutines])

    def _load_security_job_names(self) -> Set[str]:
        raw = os.getenv(
            "AUDIT_SECURITY_JOB_NAMES",
            "ptai_async_scan,semgrep_scan,sca_scan,gitleaks_scan,trivy_scan,asoc_scan,mobsf_scan",
        )
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    @staticmethod
    def _sanitize_headers_for_log(headers: Dict[str, str]) -> Dict[str, str]:
        safe = dict(headers)
        if "PRIVATE-TOKEN" in safe:
            safe["PRIVATE-TOKEN"] = "***"
        if "Authorization" in safe:
            safe["Authorization"] = "***"
        return safe

    @staticmethod
    def _read_bool_env(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _load_group_scopes(self) -> Dict[str, List[str]]:
        raw = (os.getenv("AUDIT_GROUP_SCOPES") or "").strip()
        if not raw:
            return {}

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        scopes: Dict[str, List[str]] = {}
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                host = (item.get("host") or "").strip()
                group = (item.get("group") or "").strip()
                if host and group:
                    scopes.setdefault(host, []).append(group)
        elif isinstance(payload, dict):
            for host, groups in payload.items():
                if isinstance(groups, list):
                    parsed = [str(group).strip() for group in groups if str(group).strip()]
                else:
                    parsed = [str(groups).strip()] if str(groups).strip() else []
                if host and parsed:
                    scopes[str(host)] = parsed
        return scopes

    def _load_instance_configs(self) -> List[GitLabInstanceConfig]:
        instances: List[GitLabInstanceConfig] = []
        raw_instances = runtime_setting("GITLAB_INSTANCES", "").strip()

        if raw_instances:
            try:
                payload = json.loads(raw_instances)
            except json.JSONDecodeError:
                payload = []

            if isinstance(payload, list):
                for item in payload:
                    cfg = self._instance_config_from_payload(item)
                    if cfg:
                        instances.append(cfg)
            elif isinstance(payload, dict):
                for host, item in payload.items():
                    cfg_payload = dict(item) if isinstance(item, dict) else {}
                    cfg_payload["host"] = cfg_payload.get("host") or host
                    cfg = self._instance_config_from_payload(cfg_payload)
                    if cfg:
                        instances.append(cfg)

        default_api_url = runtime_setting("GITLAB_API_URL", "").strip().rstrip("/")
        default_token = runtime_setting("GITLAB_PAT", "").strip()
        if default_api_url and default_token:
            parsed = urlparse(default_api_url)
            host = parsed.netloc or default_api_url
            if all(existing.host != host for existing in instances):
                instances.append(
                    GitLabInstanceConfig(
                        host=host,
                        api_url=default_api_url,
                        token=default_token,
                    )
                )

        return instances

    def _build_product_filter(self, product_ids: List[int]) -> Set[Tuple[str, str]]:
        if not product_ids:
            return set()
        products = self.db.query(Product).filter(Product.id.in_(product_ids)).all()
        result: Set[Tuple[str, str]] = set()
        for product in products:
            host = self._extract_host(product.gitlab_base_url).lower()
            name = (product.name or "").strip().lower()
            if host and name:
                result.add((host, name))
        return result

    def _build_scoped_targets(
        self,
        instances: List[GitLabInstanceConfig],
        products: List[Product],
    ) -> List[Tuple[GitLabInstanceConfig, str, str]]:
        instances_by_host = {item.host.lower(): item for item in instances}
        targets: List[Tuple[GitLabInstanceConfig, str, str]] = []
        seen: Set[Tuple[str, str]] = set()

        for product in products:
            host = self._extract_host(product.gitlab_base_url).lower()
            instance = instances_by_host.get(host)
            if not instance:
                logger.warning(
                    "Audit scan: product=%s skipped, instance not configured for host=%s",
                    product.name,
                    host,
                )
                continue
            scope = self._normalize_scope_from_product_path(product.path, product.name)
            if not scope:
                logger.warning("Audit scan: product=%s skipped, empty path scope", product.name)
                continue
            key = (host, scope.lower())
            if key in seen:
                continue
            seen.add(key)
            targets.append((instance, scope, product.name or "unknown"))

        return targets

    @staticmethod
    def product_group_path(path: Optional[str]) -> str:
        """Full, lower-cased GitLab group path of a product (URL paths allowed)."""
        raw = (path or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            raw = urlparse(raw).path or ""
        return "/".join(segment for segment in raw.strip("/").split("/") if segment).lower()

    @staticmethod
    def _normalize_scope_from_product_path(path: Optional[str], name: Optional[str] = None) -> str:
        raw = (path or "").strip()
        fallback_name = (name or "").strip().strip("/")
        if not raw:
            if fallback_name:
                return fallback_name.split("/", 1)[0]
            return ""
        # Allow storing full URL in product.path; extract only URL path as group scope.
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            raw = (parsed.path or "").strip()
        raw = raw.strip("/")
        if not raw:
            if fallback_name:
                return fallback_name.split("/", 1)[0]
            return ""
        segments = [item for item in raw.split("/") if item]
        if not segments:
            if fallback_name:
                return fallback_name.split("/", 1)[0]
            return ""
        if is_nested_product_namespace(segments[0]) and len(segments) >= 2:
            return f"{segments[0]}/{segments[1]}"
        return segments[0]

    @staticmethod
    def _scope_fallback_chain(scope: str) -> List[str]:
        raw = (scope or "").strip().strip("/")
        if not raw:
            return []
        segments = [segment for segment in raw.split("/") if segment]
        chain = []
        for end in range(len(segments), 0, -1):
            candidate = "/".join(segments[:end])
            if candidate not in chain:
                chain.append(candidate)
        return chain

    def _scope_resolution_candidates(self, path_or_scope: Optional[str], product_name: Optional[str]) -> List[str]:
        raw = (path_or_scope or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            raw = (parsed.path or "").strip()
        raw = raw.strip("/")

        candidates: List[str] = []
        if raw:
            for candidate in self._scope_fallback_chain(raw):
                if candidate not in candidates:
                    candidates.append(candidate)

        normalized = self._normalize_scope_from_product_path(raw, product_name)
        for candidate in self._scope_fallback_chain(normalized):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        return candidates

    def _matches_product_filter(
        self,
        instance: GitLabInstanceConfig,
        project: Dict[str, Any],
        product_filter: Set[Tuple[str, str]],
    ) -> bool:
        if not product_filter:
            return True
        name = self._derive_product_name(project.get("path_with_namespace") or "").lower()
        return (instance.host.lower(), name) in product_filter

    @staticmethod
    def _instance_config_from_payload(item: Any) -> Optional[GitLabInstanceConfig]:
        if not isinstance(item, dict):
            return None
        host = (item.get("host") or "").strip()
        api_url = (item.get("api_url") or "").strip().rstrip("/")
        token = (item.get("token") or "").strip()
        if not api_url or not token:
            return None
        if not host:
            parsed = urlparse(api_url)
            host = parsed.netloc or api_url
        return GitLabInstanceConfig(host=host, api_url=api_url, token=token)

    @staticmethod
    def _extract_host(url_or_host: Optional[str]) -> str:
        value = (url_or_host or "").strip()
        if not value:
            return ""
        parsed = urlparse(value)
        return parsed.netloc or value

    @staticmethod
    def _derive_product_name(path_with_namespace: str) -> str:
        path = (path_with_namespace or "").strip().lower()
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            return "unknown"
        if is_nested_product_namespace(segments[0]) and len(segments) >= 2:
            return segments[1]
        return segments[0]

    def _is_excluded(self, project: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        path = (project.get("path_with_namespace") or "").strip()

        if bool(project.get("archived")):
            return True, "archived project"
        if bool(project.get("empty_repo")):
            return True, "empty repository"
        if self.exclude_regex.search(path.lower()):
            return True, "matches exclude regex"

        return False, None

    @staticmethod
    def _parse_any_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
