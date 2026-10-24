from fastapi import APIRouter, Depends, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from typing import Optional, Iterable, Set, List, Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
import asyncio
import json
import logging
from ...models import (
    SessionLocal,
    Product,
    Repository,
    JobReport,
    ActivePipeline,
    PipelineJob,
    AuditCoverageStatus,
    AuditScanRun,
    ConfigScanRun,
)
from ...database import DatabaseService
from ...audit_service import AuditService
from ...config_scan_service import ConfigScanService, project_counts_for_run
from ...config_scan_reporting import findings_severity_counts, format_scan_duration, gitlab_path_from_product_field
from ...iac_service import IacService
from ...models.iac import IacScanRun
from ...models.job_report import JobStatus
from ...models.pipeline import PipelineStatus
from ...pipeline_query_service import PipelineQueryService, normalize_pipeline_filters
from ...pipeline_view_contract import (
    DEFAULT_PIPELINE_TIMEFRAME,
    PIPELINE_TIMEFRAMES,
    PipelineLifecycleGroup,
)
from ...target_navigation import compatibility_redirect_url
import math

from fastapi import APIRouter
from .common import (
    _format_duration,
    _pipeline_web_url,
    get_db,
    templates,
)

router = APIRouter(tags=["web"])
logger = logging.getLogger(__name__)


_PIPELINE_GROUP_PRESENTATION = {
    PipelineLifecycleGroup.NEEDS_ATTENTION: {
        "label": "Needs attention",
        "description": "Failed and stale pipelines in the selected period.",
    },
    PipelineLifecycleGroup.IN_PROGRESS: {
        "label": "In progress",
        "description": "Registered and polling pipelines in the selected period.",
    },
    PipelineLifecycleGroup.COMPLETED_RECENTLY: {
        "label": "Completed recently",
        "description": "Completed pipelines in the selected period.",
    },
}

_PIPELINE_TIMEFRAME_LABELS = {
    "today": "Today",
    "3d": "Last 3 days",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "1y": "Last year",
    "all": "All retained history",
}


def _pipeline_query(
    filters,
    *,
    group: PipelineLifecycleGroup | None = None,
    timeframe: str | None = None,
    clear_secondary: bool = False,
) -> str:
    params = filters.query_params(page=1)
    if group is not None:
        params["group"] = group.value
        params.pop("status", None)
    if timeframe is not None:
        params["timeframe"] = timeframe
    if clear_secondary:
        for key in ("status", "product", "repository"):
            params.pop(key, None)
        params["timeframe"] = DEFAULT_PIPELINE_TIMEFRAME
    return urlencode(params)


def _pipeline_group_summaries(filters, group_counts):
    return [
        {
            "value": group.value,
            **presentation,
            "count": group_counts.get(group.value),
            "selected": filters.group is group,
            "query": _pipeline_query(filters, group=group),
        }
        for group, presentation in _PIPELINE_GROUP_PRESENTATION.items()
    ]


def _pipeline_empty_state(filters, total_count, matching_count, group_counts):
    if matching_count:
        return None
    if total_count == 0:
        return {
            "heading": "No pipelines recorded yet",
            "body": (
                "Register a pipeline through /api/pipelines/register to start "
                "tracking it."
            ),
            "actions": [
                {"label": "Refresh pipelines", "query": ""},
            ],
        }

    selected_group_count = group_counts.get(filters.group.value, 0)
    has_secondary_filters = any(
        (
            filters.status is not None,
            filters.product_id is not None,
            filters.repository_id is not None,
        )
    )
    clear_query = _pipeline_query(filters, clear_secondary=True)
    if selected_group_count and has_secondary_filters:
        return {
            "heading": "No pipelines match these filters",
            "body": "Clear one or more filters to broaden the result.",
            "actions": [
                {"label": "Clear filters", "query": clear_query},
            ],
        }

    copy = {
        PipelineLifecycleGroup.IN_PROGRESS: (
            "No pipelines are in progress",
            "No registered or polling pipelines match the selected filters and period.",
        ),
        PipelineLifecycleGroup.NEEDS_ATTENTION: (
            "No pipelines need attention",
            "No failed or stale pipelines match the selected filters and period.",
        ),
        PipelineLifecycleGroup.COMPLETED_RECENTLY: (
            "No pipelines completed in this period",
            "Choose a longer period or clear filters to include more retained history.",
        ),
    }
    heading, body = copy[filters.group]
    actions = [
        {
            "label": "Show all retained history",
            "query": _pipeline_query(filters, timeframe="all"),
        }
    ]
    if has_secondary_filters:
        actions.append({"label": "Clear filters", "query": clear_query})
    return {"heading": heading, "body": body, "actions": actions}


def _pipeline_filter_context(filters):
    return {
        "timeframes": [
            {
                "value": value,
                "label": _PIPELINE_TIMEFRAME_LABELS[value],
                "selected": filters.timeframe == value,
            }
            for value in PIPELINE_TIMEFRAMES
        ],
        "statuses": [
            {
                "value": status.value,
                "label": status.value.replace("_", " ").title(),
                "selected": filters.status is status,
            }
            for status in PipelineStatus
        ],
        "clear_query": _pipeline_query(filters, clear_secondary=True),
    }


@router.get("/classic/pipelines", response_class=RedirectResponse, include_in_schema=False)
async def pipelines_compatibility(request: Request):
    return RedirectResponse(
        compatibility_redirect_url(request, "/pipelines"),
        status_code=308,
    )


@router.get(
    "/pipelines/operator",
    response_class=HTMLResponse,
    name="pipelines_operator",
)
async def active_pipelines(
    request: Request,
    group: str | None = None,
    status: str | None = None,
    product: str | None = None,
    repository: str | None = None,
    timeframe: str | None = None,
    page: str | None = None,
    page_size: str | None = None,
    db: Session = Depends(get_db),
):
    """Paginated Pipelines overview with server-side lifecycle filters."""
    filters = normalize_pipeline_filters(
        group=group,
        status=status,
        product=product,
        repository=repository,
        timeframe=timeframe,
        page=page,
        page_size=page_size,
    )
    try:
        result = PipelineQueryService(db).fetch(filters)
    except SQLAlchemyError:
        logger.exception("Unable to load the Pipelines page")
        return templates.TemplateResponse(
            "pipelines.html",
            {
                "request": request,
                "pipelines": [],
                "filters": filters,
                "total_count": None,
                "matching_count": None,
                "group_summaries": _pipeline_group_summaries(
                    filters,
                    {group.value: None for group in PipelineLifecycleGroup},
                ),
                "filter_context": _pipeline_filter_context(filters),
                "load_error": {
                    "heading": "Pipelines could not be loaded",
                    "body": (
                        "The retained pipeline data is temporarily unavailable. "
                        "Retry this view or return to the default Pipelines page."
                    ),
                    "retry_url": (
                        request.url.path
                        + (f"?{request.url.query}" if request.url.query else "")
                    ),
                },
                "empty_state": None,
                "pagination": {
                    "page": 1,
                    "pages": 1,
                    "page_size": filters.page_size,
                    "previous_query": None,
                    "next_query": None,
                    "size_25_query": _pipeline_query(filters),
                    "size_50_query": _pipeline_query(filters),
                },
            },
            status_code=503,
        )
    enriched = []
    for row in result.rows:
        pipeline = row.pipeline
        repo = row.repository
        product_row = row.product
        enriched.append(
            {
                "pipeline": pipeline,
                "repository": repo,
                "product": product_row,
                "job_count": row.job_count,
                "activity_at": row.activity_at,
                "pipeline_url": _pipeline_web_url(pipeline, repo, product_row),
                "repo_url": (
                    f"{product_row.gitlab_base_url.rstrip('/')}/{(repo.path or '').strip('/')}"
                    if repo and product_row and product_row.gitlab_base_url
                    else None
                ),
                "duration": _format_duration(pipeline.duration_seconds),
            }
        )

    previous_query = (
        urlencode(result.filters.query_params(page=result.filters.page - 1))
        if result.filters.page > 1
        else None
    )
    next_query = (
        urlencode(result.filters.query_params(page=result.filters.page + 1))
        if result.filters.page < result.page_count
        else None
    )
    group_counts = {
        group.value: count for group, count in result.group_counts.items()
    }
    return templates.TemplateResponse(
        "pipelines.html",
        {
            "request": request,
            "pipelines": enriched,
            "filters": result.filters,
            "total_count": result.total_count,
            "matching_count": result.matching_count,
            "group_summaries": _pipeline_group_summaries(
                result.filters,
                group_counts,
            ),
            "filter_context": _pipeline_filter_context(result.filters),
            "load_error": None,
            "empty_state": _pipeline_empty_state(
                result.filters,
                result.total_count,
                result.matching_count,
                group_counts,
            ),
            "pagination": {
                "page": result.filters.page,
                "pages": result.page_count,
                "page_size": result.filters.page_size,
                "previous_query": previous_query,
                "next_query": next_query,
                "size_25_query": urlencode(
                    result.filters.query_params(page=1, page_size=25)
                ),
                "size_50_query": urlencode(
                    result.filters.query_params(page=1, page_size=50)
                ),
            },
        },
    )
