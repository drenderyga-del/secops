from fastapi import APIRouter, Depends, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from typing import Optional, Iterable, Set, List, Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
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
from ...control_coverage import coverage_by_repository
from ...iac_service import IacService
from ...models.iac import IacScanRun
from ...models.job_report import JobStatus
from ...asoc_products import (
    asoc_product_url,
    asoc_rows_by_product,
    asoc_sync_metadata,
    report_rows,
)
from ...integration_settings import connector_snapshot, runtime_setting
from ...url_safety import safe_external_http_url
from ...target_navigation import (
    DEFAULT_TARGET_LEVEL,
    PRODUCT_TARGET_PATH,
    TARGETS_PATH,
    TARGET_LEVELS,
    compatibility_redirect_url,
    product_target_url,
    targets_url,
)
import math

from fastapi import APIRouter
from .common import (
    _format_duration_summary,
    _normalize_product_group_path,
    get_db,
    templates,
)

router = APIRouter(tags=["web"])
logger = logging.getLogger(__name__)


async def _render_repository_targets(request: Request, db: Session):
    """Render the repository mode of the canonical Targets browser."""
    db_service = DatabaseService(db)
    repositories = (
        db.query(Repository)
        .options(joinedload(Repository.product))
        .order_by(Repository.path.asc())
        .all()
    )

    rows = []
    for repo in repositories:
        stats = db_service.get_repository_stats(repo.id)
        rows.append(
            {
                "repository": repo,
                "product": repo.product,
                "stats": stats,
                "pipeline_duration": _format_duration_summary(
                    db_service.get_pipeline_duration_summary(repository_id=repo.id, timeframe="30d")
                ),
                "job_duration": _format_duration_summary(
                    db_service.get_job_duration_summary(repository_id=repo.id, timeframe="30d")
                ),
            }
        )

    return templates.TemplateResponse(
        "repositories.html",
        {
            "request": request,
            "repositories": rows,
            "target_level": "repository",
            "breadcrumbs": [
                {"label": "Security posture"},
                {"label": "Targets", "current": True},
            ],
        },
    )


async def _render_product_targets(request: Request, db: Session):
    """Render the product mode of the canonical Targets browser."""
    db_service = DatabaseService(db)
    products_data = db_service.get_products_with_stats()
    audit_service = AuditService(db)
    coverage_by_product = audit_service.get_latest_product_coverage()
    scan_service = ConfigScanService(db)

    # Batched lookups: one query each instead of 3+ per product (the per-product
    # config-scan overview parsed JSON reports from disk — the /products N+1).
    config_compact = scan_service.get_products_config_scan_compact()
    pipeline_durations = db_service.get_pipeline_duration_summaries_by_product(timeframe="30d")
    job_durations = db_service.get_job_duration_summaries_by_product(timeframe="30d")
    empty_duration = DatabaseService._summarize_durations([])
    repository_ids = [repository.id for item in products_data for repository in item["repositories"]]
    controls_by_repository = coverage_by_repository(db, repository_ids)

    for product_data in products_data:
        product_id = product_data["product"].id
        coverage = coverage_by_product.get(product_id, {"covered": 0, "total": 0, "last_scanned_at": None})
        product_data["audit_coverage"] = coverage
        p = product_data["product"]
        full_scope = gitlab_path_from_product_field(p.path)
        group_path = full_scope if full_scope else _normalize_product_group_path(p.path, p.name)
        base = (p.gitlab_base_url or "").rstrip("/")
        product_data["group_path"] = group_path
        product_data["group_url"] = f"{base}/{group_path}" if base and group_path else None

        compact = config_compact.get(product_id)
        if compact and compact["projects_scanned"]:
            product_data["config_scan_findings"] = compact["findings_total"]
            product_data["config_scan_summary"] = compact["severity"]
        else:
            product_data["config_scan_findings"] = 0
            product_data["config_scan_summary"] = None
        product_data["pipeline_duration"] = _format_duration_summary(
            pipeline_durations.get(product_id, empty_duration)
        )
        product_data["job_duration"] = _format_duration_summary(
            job_durations.get(product_id, empty_duration)
        )
        repository_coverage = [controls_by_repository.get(repository.id, {}) for repository in product_data["repositories"]]
        expected = sum(item.get("expected", 0) for item in repository_coverage)
        configured = sum(item.get("configured", 0) for item in repository_coverage)
        product_data["control_coverage"] = {
            "expected": expected,
            "configured": configured,
            "missing": max(expected - configured, 0),
        }

    return templates.TemplateResponse("products.html", {
        "request": request,
        "products": products_data,
        "target_level": "product",
        "breadcrumbs": [
            {"label": "Security posture"},
            {"label": "Targets", "current": True},
        ],
    })


@router.get("/classic/targets", response_class=RedirectResponse, include_in_schema=False)
async def targets_legacy_compatibility(request: Request):
    return RedirectResponse(
        compatibility_redirect_url(request, "/targets/operator"),
        status_code=308,
    )


@router.get(
    "/targets/operator",
    response_class=HTMLResponse,
    name="targets_operator",
)
async def targets_collection(
    request: Request,
    level: str = DEFAULT_TARGET_LEVEL,
    db: Session = Depends(get_db),
):
    normalized_level = level if level in TARGET_LEVELS else DEFAULT_TARGET_LEVEL
    if normalized_level == "repository":
        return await _render_repository_targets(request, db)
    return await _render_product_targets(request, db)


@router.get("/products", response_class=RedirectResponse)
async def products_compatibility(request: Request):
    return RedirectResponse(
        compatibility_redirect_url(
            request,
            TARGETS_PATH,
            forced_query={"level": "product"},
        ),
        status_code=308,
    )


@router.get("/repositories", response_class=RedirectResponse)
async def repositories_compatibility(request: Request):
    return RedirectResponse(
        compatibility_redirect_url(
            request,
            TARGETS_PATH,
            forced_query={"level": "repository"},
        ),
        status_code=308,
    )


@router.get(
    PRODUCT_TARGET_PATH + "/operator",
    response_class=HTMLResponse,
    name="product_target_operator",
)
async def product_target_operator(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
):
    """Operator workbench for one monitored product group.

    The canonical product drill-down is the React view at PRODUCT_TARGET_PATH;
    this page keeps per-repository Config Guard actions and duration detail.
    """
    db_service = DatabaseService(db)
    scan_service = ConfigScanService(db)

    # Get product
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    asoc_cards = asoc_rows_by_product(report_rows(db)).get(product.id, [])
    asoc_base_url = runtime_setting("HEXWAY_URL", "", db=db)
    for card in asoc_cards:
        card["asoc_url"] = safe_external_http_url(
            asoc_product_url(card.get("asoc_id"), card.get("slug"), base_url=asoc_base_url)
        )
        card["target_groups"] = [
            {
                "id": target.id,
                "name": target.name,
                "path": gitlab_path_from_product_field(target.path)
                or _normalize_product_group_path(target.path, target.name),
                "url": safe_external_http_url(
                    f"{target.gitlab_base_url.rstrip('/')}/{gitlab_path_from_product_field(target.path) or _normalize_product_group_path(target.path, target.name)}"
                    if target.gitlab_base_url
                    else None
                ),
                "repositories": card.get("monitor_repository_counts", {}).get(str(target.id), 0),
            }
            for target in card.get("monitor_products", [])
        ]
    asoc_sync = asoc_sync_metadata(
        db,
        configured=bool(connector_snapshot(db, "asoc").get("configured")),
    )

    config_scan_repo_map, config_scan_run_id = scan_service.get_latest_succeeded_config_scan_repo_map(
        product_id
    )

    # Get repositories for this product
    repositories = db.query(Repository).filter(Repository.product_id == product_id).all()
    controls_by_repository = coverage_by_repository(db, [repository.id for repository in repositories])

    # Get job counts and stats for each repository
    repos_with_jobs = []
    for repo in repositories:
        job_reports = db_service.get_repository_jobs(repo.id)
        latest_jobs = job_reports[:5]  # Get latest 5 jobs

        # Get repository stats including success rate
        repo_stats = db_service.get_repository_stats(repo.id)
        pipeline_duration = _format_duration_summary(
            db_service.get_pipeline_duration_summary(repository_id=repo.id, timeframe="30d")
        )
        job_duration = _format_duration_summary(
            db_service.get_job_duration_summary(repository_id=repo.id, timeframe="30d")
        )

        path_key = (repo.path or "").strip().strip("/").lower()
        cscan_entry = config_scan_repo_map.get(path_key) if path_key else None

        repos_with_jobs.append({
            "repository": repo,
            "latest_jobs": latest_jobs,
            "total_jobs": len(job_reports),
            "stats": repo_stats,
            "pipeline_duration": pipeline_duration,
            "job_duration": job_duration,
            "config_scan": cscan_entry,
            "control_coverage": controls_by_repository.get(
                repo.id, {"expected": 0, "configured": 0, "missing": 0, "excluded": 0}
            ),
        })

    product_group_path = gitlab_path_from_product_field(product.path) or _normalize_product_group_path(
        product.path, product.name
    )
    product_group_url = safe_external_http_url(
        f"{product.gitlab_base_url.rstrip('/')}/{product_group_path}"
        if product.gitlab_base_url and product_group_path
        else None
    )
    canonical_card = asoc_cards[0] if len(asoc_cards) == 1 else None
    product_identity_title = (
        canonical_card.get("title") if canonical_card else None
    ) or product.name
    product_identity_bi_id = canonical_card.get("bi_id") if canonical_card else None
    product_identity_asoc_url = canonical_card.get("asoc_url") if canonical_card else None

    return templates.TemplateResponse("product_repositories.html", {
        "request": request,
        "product": product,
        "product_group_path": product_group_path,
        "product_group_url": product_group_url,
        "product_identity_title": product_identity_title,
        "product_identity_bi_id": product_identity_bi_id,
        "product_identity_asoc_url": product_identity_asoc_url,
        "repositories": repos_with_jobs,
        "asoc_cards": asoc_cards,
        "asoc_sync": asoc_sync,
        "config_scan_run_id": config_scan_run_id,
        "pipeline_duration": _format_duration_summary(
            db_service.get_pipeline_duration_summary(product_id=product_id, timeframe="30d")
        ),
        "job_duration": _format_duration_summary(
            db_service.get_job_duration_summary(product_id=product_id, timeframe="30d")
        ),
        "breadcrumbs": [
            {"label": "Security posture"},
            {"label": "Targets", "href": targets_url()},
            {"label": product_identity_title, "current": True},
        ],
    })


@router.get("/products/{product_id}", response_class=RedirectResponse)
async def product_target_compatibility(request: Request, product_id: int):
    return RedirectResponse(
        compatibility_redirect_url(request, product_target_url(product_id)),
        status_code=308,
    )


@router.post("/products/{product_id}/delete")
async def delete_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(product)
    db.commit()
    return RedirectResponse(url="/targets/operator?level=product", status_code=303)


@router.post("/products/cleanup-duplicates")
async def cleanup_products_duplicates(db: Session = Depends(get_db)):
    db_service = DatabaseService(db)
    db_service.cleanup_duplicate_products()
    return RedirectResponse(
        url="/targets/operator?level=product#maintenance",
        status_code=303,
    )


@router.post("/products/normalize-paths")
async def normalize_products_paths(db: Session = Depends(get_db)):
    db_service = DatabaseService(db)
    normalized = db_service.normalize_product_paths()
    logger.info("Products path normalization finished: updated=%s", normalized)
    return RedirectResponse(
        url="/targets/operator?level=product#maintenance",
        status_code=303,
    )
