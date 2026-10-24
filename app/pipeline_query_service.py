"""Server-side query model for the Pipelines page."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

from sqlalchemy import case, exists, func
from sqlalchemy.orm import Session, aliased

from .models import ActivePipeline, PipelineJob, Product, Repository
from .models.pipeline import PipelineStatus
from .pipeline_view_contract import (
    DEFAULT_PIPELINE_GROUP,
    DEFAULT_PIPELINE_TIMEFRAME,
    PIPELINE_STATUS_GROUP,
    PIPELINE_TIMEFRAMES,
    PipelineLifecycleGroup,
    group_for_pipeline_status,
)


PIPELINE_PAGE_SIZES = (25, 50, 100)
DEFAULT_PIPELINE_PAGE_SIZE = 25


@dataclass(frozen=True)
class PipelineFilters:
    group: PipelineLifecycleGroup = DEFAULT_PIPELINE_GROUP
    status: PipelineStatus | None = None
    product_id: int | None = None
    repository_id: int | None = None
    timeframe: str = DEFAULT_PIPELINE_TIMEFRAME
    page: int = 1
    page_size: int = DEFAULT_PIPELINE_PAGE_SIZE

    def query_params(self, *, page: int | None = None, page_size: int | None = None) -> dict[str, str]:
        values = {
            "group": self.group.value,
            "timeframe": self.timeframe,
            "page": str(page if page is not None else self.page),
            "page_size": str(page_size if page_size is not None else self.page_size),
        }
        if self.status is not None:
            values["status"] = self.status.value
        if self.product_id is not None:
            values["product"] = str(self.product_id)
        if self.repository_id is not None:
            values["repository"] = str(self.repository_id)
        return values


@dataclass(frozen=True)
class PipelineRow:
    pipeline: ActivePipeline
    repository: Repository | None
    product: Product | None
    job_count: int
    activity_at: datetime


@dataclass(frozen=True)
class PipelineQueryResult:
    rows: list[PipelineRow]
    filters: PipelineFilters
    total_count: int
    matching_count: int
    group_counts: dict[PipelineLifecycleGroup, int]
    page_count: int
    request_now: datetime


@dataclass(frozen=True)
class PipelineExecutionHealth:
    """Terminal pipeline executions in one observation window."""

    timeframe: str
    completed: int
    failed: int
    total: int
    success_rate: float
    failure_rate: float


@dataclass(frozen=True)
class PipelineProductFilterOption:
    id: int
    name: str
    path: str


@dataclass(frozen=True)
class PipelineRepositoryFilterOption:
    id: int
    path: str
    product_id: int


@dataclass(frozen=True)
class PipelineFilterOptions:
    products: list[PipelineProductFilterOption]
    repositories: list[PipelineRepositoryFilterOption]


def normalize_pipeline_filters(
    *,
    group: str | None = None,
    status: str | None = None,
    product: str | int | None = None,
    repository: str | int | None = None,
    timeframe: str | None = None,
    page: str | int | None = None,
    page_size: str | int | None = None,
) -> PipelineFilters:
    normalized_status = _enum_or_none(PipelineStatus, status)
    normalized_group = _enum_or_none(PipelineLifecycleGroup, group)
    if normalized_group is None:
        normalized_group = (
            group_for_pipeline_status(normalized_status)
            if group is None and normalized_status is not None
            else DEFAULT_PIPELINE_GROUP
        )

    normalized_timeframe = (timeframe or "").strip().lower()
    if normalized_timeframe not in PIPELINE_TIMEFRAMES:
        normalized_timeframe = DEFAULT_PIPELINE_TIMEFRAME

    normalized_page_size = _positive_int(page_size)
    if normalized_page_size not in PIPELINE_PAGE_SIZES:
        normalized_page_size = DEFAULT_PIPELINE_PAGE_SIZE

    return PipelineFilters(
        group=normalized_group,
        status=normalized_status,
        product_id=_positive_int(product),
        repository_id=_positive_int(repository),
        timeframe=normalized_timeframe,
        page=_positive_int(page) or 1,
        page_size=normalized_page_size,
    )


class PipelineQueryService:
    """Execute the complete Pipelines list/count contract with fixed query cost."""

    def __init__(self, db: Session):
        self.db = db

    def execution_health(
        self,
        timeframe: str,
        *,
        request_now: datetime | None = None,
    ) -> PipelineExecutionHealth:
        """Count completed and failed pipeline executions using list semantics."""
        normalized_timeframe = normalize_pipeline_filters(timeframe=timeframe).timeframe
        now = _as_utc(request_now or datetime.now(timezone.utc))
        activity_at = self.activity_at_expression()
        query = self.db.query(
            ActivePipeline.status,
            func.count(ActivePipeline.id),
        ).filter(
            ActivePipeline.status.in_(
                (PipelineStatus.COMPLETED, PipelineStatus.FAILED)
            )
        )
        query = self._apply_timeframe(
            query,
            activity_at,
            normalized_timeframe,
            now,
        ).group_by(ActivePipeline.status)
        counts = {status: int(count) for status, count in query.all()}
        completed = counts.get(PipelineStatus.COMPLETED, 0)
        failed = counts.get(PipelineStatus.FAILED, 0)
        total = completed + failed
        return PipelineExecutionHealth(
            timeframe=normalized_timeframe,
            completed=completed,
            failed=failed,
            total=total,
            success_rate=round((completed / total) * 100, 1) if total else 0,
            failure_rate=round((failed / total) * 100, 1) if total else 0,
        )

    def fetch(
        self,
        filters: PipelineFilters,
        *,
        request_now: datetime | None = None,
    ) -> PipelineQueryResult:
        now = _as_utc(request_now or datetime.now(timezone.utc))
        activity_at = self.activity_at_expression()

        total_count = int(self.db.query(func.count(ActivePipeline.id)).scalar() or 0)

        matching_query = self.db.query(func.count(ActivePipeline.id))
        matching_query = self._apply_scope_filters(matching_query, filters)
        matching_query = self._apply_timeframe(matching_query, activity_at, filters.timeframe, now)
        matching_query = matching_query.filter(
            ActivePipeline.status.in_(_statuses_for_group(filters.group))
        )
        if filters.status is not None:
            matching_query = matching_query.filter(ActivePipeline.status == filters.status)
        matching_count = int(matching_query.scalar() or 0)

        page_count = max(1, ceil(matching_count / filters.page_size))
        effective_filters = replace(
            filters,
            page=min(filters.page, page_count) if matching_count else 1,
        )

        group_counts_query = self.db.query(ActivePipeline.status, func.count(ActivePipeline.id))
        group_counts_query = self._apply_scope_filters(group_counts_query, effective_filters)
        group_counts_query = self._apply_timeframe(
            group_counts_query,
            activity_at,
            effective_filters.timeframe,
            now,
        ).group_by(ActivePipeline.status)
        group_counts = {group: 0 for group in PipelineLifecycleGroup}
        for status_value, count in group_counts_query.all():
            group_counts[group_for_pipeline_status(status_value)] += int(count)

        job_counts = (
            self.db.query(
                PipelineJob.active_pipeline_id.label("pipeline_id"),
                func.count(PipelineJob.id).label("job_count"),
            )
            .group_by(PipelineJob.active_pipeline_id)
            .subquery()
        )
        rows_query = (
            self.db.query(
                ActivePipeline,
                Repository,
                Product,
                func.coalesce(job_counts.c.job_count, 0),
                activity_at.label("activity_at"),
            )
            .outerjoin(Repository, Repository.id == ActivePipeline.repository_id)
            .outerjoin(Product, Product.id == ActivePipeline.product_id)
            .outerjoin(job_counts, job_counts.c.pipeline_id == ActivePipeline.id)
        )
        rows_query = self._apply_scope_filters(rows_query, effective_filters)
        rows_query = self._apply_timeframe(
            rows_query,
            activity_at,
            effective_filters.timeframe,
            now,
        ).filter(ActivePipeline.status.in_(_statuses_for_group(effective_filters.group)))
        if effective_filters.status is not None:
            rows_query = rows_query.filter(ActivePipeline.status == effective_filters.status)
        rows_query = (
            rows_query.order_by(activity_at.desc(), ActivePipeline.id.desc())
            .offset((effective_filters.page - 1) * effective_filters.page_size)
            .limit(effective_filters.page_size)
        )

        rows = [
            PipelineRow(
                pipeline=pipeline,
                repository=repository,
                product=product,
                job_count=int(job_count),
                activity_at=row_activity_at,
            )
            for pipeline, repository, product, job_count, row_activity_at in rows_query.all()
        ]
        return PipelineQueryResult(
            rows=rows,
            filters=effective_filters,
            total_count=total_count,
            matching_count=matching_count,
            group_counts=group_counts,
            page_count=page_count,
            request_now=now,
        )

    def filter_options(self) -> PipelineFilterOptions:
        """Return target choices that are backed by retained pipeline records."""
        product_rows = (
            self.db.query(Product.id, Product.name, Product.path)
            .join(
                ActivePipeline,
                ActivePipeline.product_id == Product.id,
            )
            .distinct()
            .order_by(Product.name.asc(), Product.id.asc())
            .all()
        )
        repository_rows = (
            self.db.query(
                Repository.id,
                Repository.path,
                Repository.product_id,
            )
            .join(
                ActivePipeline,
                ActivePipeline.repository_id == Repository.id,
            )
            .distinct()
            .order_by(Repository.path.asc(), Repository.id.asc())
            .all()
        )
        return PipelineFilterOptions(
            products=[
                PipelineProductFilterOption(
                    id=int(product_id),
                    name=name,
                    path=path,
                )
                for product_id, name, path in product_rows
            ],
            repositories=[
                PipelineRepositoryFilterOption(
                    id=int(repository_id),
                    path=path,
                    product_id=int(product_id),
                )
                for repository_id, path, product_id in repository_rows
            ],
        )

    @staticmethod
    def activity_at_expression():
        return case(
            (
                ActivePipeline.status.in_(
                    (PipelineStatus.REGISTERED, PipelineStatus.POLLING)
                ),
                func.coalesce(
                    ActivePipeline.last_polled_at,
                    ActivePipeline.updated_at,
                    ActivePipeline.created_at,
                ),
            ),
            (
                ActivePipeline.status.in_(
                    (PipelineStatus.COMPLETED, PipelineStatus.FAILED)
                ),
                func.coalesce(
                    ActivePipeline.completed_at,
                    ActivePipeline.finished_at,
                    ActivePipeline.updated_at,
                    ActivePipeline.created_at,
                ),
            ),
            (
                ActivePipeline.status == PipelineStatus.STALE,
                func.coalesce(
                    ActivePipeline.stale_at,
                    ActivePipeline.last_polled_at,
                    ActivePipeline.updated_at,
                    ActivePipeline.created_at,
                ),
            ),
            else_=ActivePipeline.created_at,
        )

    @staticmethod
    def _apply_scope_filters(query: Any, filters: PipelineFilters):
        if filters.product_id is not None:
            query = query.filter(ActivePipeline.product_id == filters.product_id)
        if filters.repository_id is not None:
            query = query.filter(ActivePipeline.repository_id == filters.repository_id)
        if filters.product_id is not None and filters.repository_id is not None:
            scope_repository = aliased(Repository)
            query = query.filter(
                exists().where(
                    scope_repository.id == filters.repository_id,
                    scope_repository.product_id == filters.product_id,
                )
            )
        return query

    @staticmethod
    def _apply_timeframe(query: Any, activity_at: Any, timeframe: str, now: datetime):
        cutoff = _timeframe_cutoff(timeframe, now)
        if cutoff is None:
            return query
        return query.filter(activity_at >= cutoff, activity_at < now)


def _statuses_for_group(group: PipelineLifecycleGroup) -> tuple[PipelineStatus, ...]:
    return tuple(
        status for status, mapped_group in PIPELINE_STATUS_GROUP.items() if mapped_group is group
    )


def _timeframe_cutoff(timeframe: str, now: datetime) -> datetime | None:
    if timeframe == "all":
        return None
    if timeframe == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = {"3d": 3, "7d": 7, "30d": 30, "1y": 365}[timeframe]
    return now - timedelta(days=days)


def _positive_int(value: str | int | None) -> int | None:
    try:
        normalized = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return normalized if normalized is not None and normalized > 0 else None


def _enum_or_none(enum_type, value):
    if value is None:
        return None
    try:
        return enum_type(str(value).strip().lower())
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
