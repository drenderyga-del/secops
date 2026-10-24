from fastapi import APIRouter, Depends, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
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
from ...audit_service import AuditService, RepositoryAttribution
from ...config_scan_service import ConfigScanService, project_counts_for_run
from ...config_scan_reporting import findings_severity_counts, format_scan_duration, gitlab_path_from_product_field
from ...iac_service import IacService
from ...models.iac import IacScanRun
from ...models.job_report import JobStatus
from ...target_navigation import (
    COVERAGE_PATH,
    COVERAGE_RUNS_PATH,
    PRODUCT_COVERAGE_PATH,
    compatibility_redirect_url,
    coverage_run_url,
    product_coverage_url,
)
import math

from fastapi import APIRouter
from .common import (
    _build_repository_target_index,
    _normalize_product_group_path,
    get_db,
    templates,
)

router = APIRouter(tags=["web"])
logger = logging.getLogger(__name__)

_audit_task_registry: dict[int, asyncio.Task] = {}

_COVERAGE_STATUS_FILTERS = {"all", "active", "not_configured", "inactive"}
_AUDIT_OPERATOR_QUERY_KEYS = frozenset(
    {
        "only_problems",
        "selected_product_ids",
        "status",
        "status_filter",
        "run_id",
    }
)
_COVERAGE_METRIC_PRESENTATION = (
    (
        "all",
        "Total",
        "All monitored repositories",
        "",
    ),
    (
        "active",
        "Active",
        "Pipelines and security jobs detected",
        "audit-metric-active",
    ),
    (
        "not_configured",
        "Not Configured",
        "Pipelines detected, security job missing",
        "audit-metric-missing",
    ),
    (
        "inactive",
        "Inactive",
        "No pipelines or GitLab unavailable",
        "audit-metric-inactive",
    ),
)


def _normalize_coverage_filters(
    status: Optional[str],
    only_problems: bool,
) -> tuple[str, bool]:
    normalized_status = (status or "all").strip().lower()
    if normalized_status not in _COVERAGE_STATUS_FILTERS:
        normalized_status = "all"
    # A concrete status is the more specific choice. Generated Problems-only
    # URLs always set status=all, so the two controls cannot overlap.
    if normalized_status != "all":
        only_problems = False
    return normalized_status, only_problems


def _coverage_status_key(status: AuditCoverageStatus) -> str:
    if status == AuditCoverageStatus.COVERED:
        return "active"
    if status == AuditCoverageStatus.PIPELINE_NO_SEC:
        return "not_configured"
    if status in (
        AuditCoverageStatus.NO_PIPELINES,
        AuditCoverageStatus.INACCESSIBLE,
    ):
        return "inactive"
    return "all"


def _coverage_counts(rows: Iterable[dict]) -> dict[str, int]:
    counts = {
        "total": 0,
        "active": 0,
        "not_configured": 0,
        "inactive": 0,
    }
    for row in rows:
        key = _coverage_status_key(row["snapshot"].status)
        if key == "all":
            continue
        counts["total"] += 1
        counts[key] += 1
    return counts


def _filter_coverage_rows(
    rows: Iterable[dict],
    status_filter: str,
    only_problems: bool,
) -> list[dict]:
    if status_filter != "all":
        return [
            row
            for row in rows
            if _coverage_status_key(row["snapshot"].status) == status_filter
        ]
    if only_problems:
        return [
            row
            for row in rows
            if _coverage_status_key(row["snapshot"].status)
            in {"not_configured", "inactive"}
        ]
    return list(rows)


def _coverage_metric_cards(
    counts: dict[str, int],
    *,
    base_url: str,
    status_filter: str,
    only_problems: bool,
    persistent_query: Optional[dict[str, str]] = None,
) -> list[dict]:
    cards = []
    for value, label, description, class_name in _COVERAGE_METRIC_PRESENTATION:
        params = dict(persistent_query or {})
        params.update({"status": value, "only_problems": "false"})
        count_key = "total" if value == "all" else value
        cards.append(
            {
                "value": value,
                "label": label,
                "description": description,
                "class_name": class_name,
                "count": counts[count_key],
                "href": f"{base_url}?{urlencode(params)}",
                "selected": not only_problems and status_filter == value,
            }
        )
    return cards


_COVERAGE_RUN_STATUS = {
    "queued": ("Queued", "running"),
    "running": ("Running", "running"),
    "completed": ("Completed", "success"),
    "cancelled": ("Cancelled", "warning"),
    "failed": ("Failed", "error"),
}


def _diagnostic_excerpt(value: Optional[str], limit: int = 180) -> str:
    normalized = " ".join((value or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


def _format_run_duration(run: AuditScanRun) -> Optional[str]:
    if not run.started_at:
        return None
    finished_at = run.finished_at or (
        datetime.now(timezone.utc) if run.status == "running" else None
    )
    if not finished_at:
        return None
    started_at = (
        run.started_at
        if run.started_at.tzinfo
        else run.started_at.replace(tzinfo=timezone.utc)
    )
    finished_at = (
        finished_at
        if finished_at.tzinfo
        else finished_at.replace(tzinfo=timezone.utc)
    )
    seconds = max(0, int((finished_at - started_at).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _serialize_scan_run(run: Optional[AuditScanRun]) -> Optional[dict]:
    if not run:
        return None
    selected_ids = [
        int(item.strip())
        for item in (run.requested_product_ids or "").split(",")
        if item.strip().isdigit()
    ]
    diagnostic = run.error_message or run.message or ""
    status_label, status_class = _COVERAGE_RUN_STATUS.get(
        run.status,
        (run.status.replace("_", " ").title(), "unknown"),
    )
    return {
        "id": run.id,
        "url": coverage_run_url(run.id),
        "status": run.status,
        "status_label": status_label,
        "status_class": status_class,
        "selected_product_ids": selected_ids,
        "selected_products": run.selected_products or 0,
        "processed_repositories": run.processed_repositories or 0,
        "total_repositories": run.total_repositories or 0,
        "message": run.message,
        "error_message": run.error_message,
        "diagnostic": diagnostic,
        "diagnostic_excerpt": _diagnostic_excerpt(diagnostic),
        "diagnostic_is_truncated": len(" ".join(diagnostic.split())) > 180,
        "duration": _format_run_duration(run),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


async def _run_audit_scan_worker(run_id: int, product_ids: List[int]):
    scan_db = SessionLocal()
    status_db = SessionLocal()
    try:
        logger.info("Audit task %s started, selected_products=%s", run_id, product_ids or "all")
        run = status_db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
        if not run:
            return
        if run.status == "cancelled":
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.message = "Collecting repositories..."
        status_db.add(run)
        status_db.commit()

        service = AuditService(scan_db)
        progress_state = {"last_saved": -1}

        async def _progress(processed: int, total: int, repo_path: Optional[str]):
            should_save = (
                processed in (0, total)
                or progress_state["last_saved"] < 0
                or (processed - progress_state["last_saved"]) >= 5
            )
            if not should_save:
                return
            current = status_db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
            if not current or current.status == "cancelled":
                return
            current.status = "running"
            current.total_repositories = total
            current.processed_repositories = processed
            if total == 0:
                current.message = repo_path or "Collecting repositories..."
            elif repo_path:
                current.message = f"Scanning {processed}/{total}: {repo_path}"
            else:
                current.message = f"Scanning {processed}/{total} repositories"
            status_db.add(current)
            status_db.commit()
            progress_state["last_saved"] = processed

        result = await service.run_scan(product_ids=product_ids, progress_callback=_progress)
        done = result.get("total", 0)
        current = status_db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
        if current and current.status != "cancelled":
            current.status = "completed"
            current.total_repositories = done
            current.processed_repositories = done
            current.finished_at = datetime.now(timezone.utc)
            current.message = f"Completed: scanned {done} repositories"
            current.error_message = None
            status_db.add(current)
            status_db.commit()
            logger.info("Audit task %s completed: scanned=%s", run_id, done)
    except asyncio.CancelledError:
        cancelled = status_db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
        if cancelled:
            cancelled.status = "cancelled"
            cancelled.finished_at = datetime.now(timezone.utc)
            cancelled.message = "Audit scan cancelled by user"
            cancelled.error_message = None
            status_db.add(cancelled)
            status_db.commit()
        logger.warning("Audit task %s cancelled by user", run_id)
        raise
    except Exception as exc:
        failed = status_db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
        if failed and failed.status != "cancelled":
            failed.status = "failed"
            failed.finished_at = datetime.now(timezone.utc)
            failed.error_message = str(exc)
            failed.message = "Audit scan failed"
            status_db.add(failed)
            status_db.commit()
        logger.exception("Audit task %s failed: %s", run_id, exc)
    finally:
        scan_db.close()
        status_db.close()


def _schedule_audit_scan(run_id: int, product_ids: List[int]):
    async def _worker():
        await _run_audit_scan_worker(run_id, product_ids)

    task = asyncio.create_task(_worker())
    _audit_task_registry[run_id] = task

    def _cleanup(_):
        _audit_task_registry.pop(run_id, None)

    task.add_done_callback(_cleanup)


def _schedule_audit_scan_batch(run_items: List[tuple[int, int]]):
    async def _batch_worker():
        for run_id, product_id in run_items:
            status_db = SessionLocal()
            try:
                run = status_db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
                if not run or run.status == "cancelled":
                    continue
            finally:
                status_db.close()

            task = asyncio.create_task(_run_audit_scan_worker(run_id, [product_id]))
            _audit_task_registry[run_id] = task
            try:
                await task
            except asyncio.CancelledError:
                # Cancelling one run from a multi-product batch must not leave the
                # remaining queued runs stuck forever.
                continue
            finally:
                _audit_task_registry.pop(run_id, None)

    asyncio.create_task(_batch_worker())

@router.get("/classic/coverage", response_class=RedirectResponse, include_in_schema=False)
async def coverage_classic_compatibility(request: Request):
    return RedirectResponse(
        compatibility_redirect_url(request, "/coverage/operator"),
        status_code=308,
    )


@router.get(
    "/coverage/operator",
    response_class=HTMLResponse,
    name="coverage_operator",
)
async def coverage_view(
    request: Request,
    only_problems: bool = False,
    selected_product_ids: str = "",
    status: Optional[str] = None,
    status_filter: Optional[str] = None,
    scan_started: bool = False,
    run_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    coverage_view_url = (
        "/coverage/operator"
        if request.url.path == "/coverage/operator"
        else COVERAGE_PATH
    )
    status_filter, only_problems = _normalize_coverage_filters(
        status if status is not None else status_filter,
        only_problems,
    )

    audit_service = AuditService(db)
    summary = audit_service.get_latest_summary()
    rows = audit_service.get_latest_rows(only_problems=False, stop_window_days=14)
    products = db.query(Product).order_by(Product.name.asc()).all()
    selected_ids = sorted({
        int(item) for item in (selected_product_ids or "").split(",")
        if item.strip().isdigit()
    })
    compact = {
        "total": summary["covered"] + summary["pipeline_no_sec"] + summary["no_pipelines"] + summary["inaccessible"],
        "secpipe_working": summary["covered"],
        "secpipe_missing": summary["pipeline_no_sec"],
        "secpipe_present_not_working": summary["no_pipelines"] + summary["inaccessible"],
    }
    coverage_counts = {
        "total": compact["total"],
        "active": compact["secpipe_working"],
        "not_configured": compact["secpipe_missing"],
        "inactive": compact["secpipe_present_not_working"],
    }
    if status_filter == "all":
        matching_count = (
            coverage_counts["not_configured"] + coverage_counts["inactive"]
            if only_problems
            else coverage_counts["total"]
        )
    else:
        matching_count = coverage_counts[status_filter]
    coverage_filter_active = only_problems or status_filter != "all"

    attribution = RepositoryAttribution(db)
    grouped_map = {}

    for row in rows:
        repo = row["repository"]
        snapshot = row["snapshot"]
        repo_path = repo.path_with_namespace or ""
        product = attribution.product_for(repo.instance_host, repo_path)

        if product:
            group_key = f"product:{product.id}"
            group_label = product.name
            group_path = _normalize_product_group_path(product.path, product.name)
            group_url = f"{(product.gitlab_base_url or '').rstrip('/')}/{group_path}" if product.gitlab_base_url else None
        else:
            group_key = f"host:{repo.instance_host}:{repo.path_with_namespace}"
            group_label = repo.path_with_namespace or "unmapped"
            group_path = None
            group_url = None

        if group_key not in grouped_map:
            grouped_map[group_key] = {
                "key": group_key,
                "product": product,
                "label": group_label,
                "group_path": group_path,
                "group_url": group_url,
                "rows": [],
                "counts": {
                    "total": 0,
                    "active": 0,
                    "not_configured": 0,
                    "inactive": 0,
                    "excluded": 0,
                },
            }

        group = grouped_map[group_key]
        group["rows"].append(row)
        status_key = _coverage_status_key(snapshot.status)
        if status_key == "all":
            group["counts"]["excluded"] += 1
        else:
            group["counts"]["total"] += 1
            group["counts"][status_key] += 1

    product_cards = []
    for product in products:
        group_path = _normalize_product_group_path(product.path, product.name)
        group_url = f"{(product.gitlab_base_url or '').rstrip('/')}/{group_path}" if product.gitlab_base_url else None
        product_cards.append(
            {
                "product": product,
                "group_path": group_path,
                "group_url": group_url,
                "counts": {
                    "total": 0,
                    "active": 0,
                    "not_configured": 0,
                    "inactive": 0,
                    "excluded": 0,
                },
            }
        )
    card_by_id = {card["product"].id: card for card in product_cards}

    for group in grouped_map.values():
        product = group.get("product")
        if not product or product.id not in card_by_id:
            continue
        card = card_by_id[product.id]
        card["counts"] = group["counts"]

    if status_filter == "active":
        product_cards = [card for card in product_cards if card["counts"]["active"] > 0]
    elif status_filter == "not_configured":
        product_cards = [card for card in product_cards if card["counts"]["not_configured"] > 0]
    elif status_filter == "inactive":
        product_cards = [card for card in product_cards if card["counts"]["inactive"] > 0]
    elif only_problems:
        product_cards = [
            card for card in product_cards
            if (card["counts"]["not_configured"] + card["counts"]["inactive"]) > 0
        ]
    else:
        product_cards = [card for card in product_cards if card["counts"]["total"] > 0]

    product_cards = sorted(product_cards, key=lambda item: item["product"].name.lower())

    status_meta = {
        AuditCoverageStatus.COVERED.value: {"label": "Active", "class": "success", "icon": "check-circle"},
        AuditCoverageStatus.PIPELINE_NO_SEC.value: {"label": "Not Configured", "class": "error", "icon": "warning"},
        AuditCoverageStatus.NO_PIPELINES.value: {"label": "Inactive", "class": "unknown", "icon": "clock"},
        AuditCoverageStatus.INACCESSIBLE.value: {"label": "Inaccessible", "class": "cancelled", "icon": "x-circle"},
        AuditCoverageStatus.EXCLUDED.value: {"label": "Excluded", "class": "skipped", "icon": "stop"},
    }
    latest_scan_run = None
    if run_id:
        latest_scan_run = (
            db.query(AuditScanRun)
            .filter(
                AuditScanRun.id == run_id,
                AuditScanRun.status.in_(["running", "queued"]),
            )
            .first()
        )
    if not latest_scan_run:
        latest_scan_run = (
            db.query(AuditScanRun)
            .filter(AuditScanRun.status.in_(["running", "queued"]))
            .order_by(AuditScanRun.id.asc())
            .first()
        )
    if not latest_scan_run:
        latest_scan_run = (
            db.query(AuditScanRun)
            .order_by(AuditScanRun.id.desc())
            .first()
        )
    scan_run = _serialize_scan_run(latest_scan_run)

    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request,
            "summary": summary,
            "compact": compact,
            "coverage_metrics": _coverage_metric_cards(
                coverage_counts,
                base_url=coverage_view_url,
                status_filter=status_filter,
                only_problems=only_problems,
                persistent_query=(
                    {"selected_product_ids": ",".join(str(item) for item in selected_ids)}
                    if selected_ids
                    else None
                ),
            ),
            "coverage_filter_active": coverage_filter_active,
            "matching_count": matching_count,
            "rows": rows,
            "product_cards": product_cards,
            "status_meta": status_meta,
            "only_problems": only_problems,
            "status_filter": status_filter,
            "products": products,
            "selected_product_ids": selected_ids,
            "scan_run": scan_run,
            "scan_started": scan_started,
            "run_id": run_id,
            "coverage_url": coverage_view_url,
            "breadcrumbs": [
                {"label": "Security posture"},
                {"label": "Coverage", "current": True},
            ],
        },
    )


@router.get(
    COVERAGE_RUNS_PATH,
    response_class=HTMLResponse,
    name="coverage_run_history",
)
async def coverage_run_history(
    request: Request,
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    page_size = 20
    total = db.query(func.count(AuditScanRun.id)).scalar() or 0
    pages = max(1, math.ceil(total / page_size)) if total else 1
    page = min(page, pages)
    runs = (
        db.query(AuditScanRun)
        .order_by(AuditScanRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return templates.TemplateResponse(
        "coverage_runs.html",
        {
            "request": request,
            "runs": [_serialize_scan_run(run) for run in runs],
            "pagination": {
                "page": page,
                "pages": pages,
                "total": total,
                "previous_url": (
                    f"{COVERAGE_RUNS_PATH}?{urlencode({'page': page - 1})}"
                    if page > 1
                    else None
                ),
                "next_url": (
                    f"{COVERAGE_RUNS_PATH}?{urlencode({'page': page + 1})}"
                    if page < pages
                    else None
                ),
            },
            "breadcrumbs": [
                {"label": "Security posture"},
                {"label": "Coverage", "href": COVERAGE_PATH},
                {"label": "Run history", "current": True},
            ],
        },
    )


@router.get(
    f"{COVERAGE_RUNS_PATH}/{{run_id}}",
    response_class=HTMLResponse,
    name="coverage_run_detail",
)
async def coverage_run_detail(
    request: Request,
    run_id: int,
    db: Session = Depends(get_db),
):
    run = db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Coverage run not found")
    return templates.TemplateResponse(
        "coverage_run_detail.html",
        {
            "request": request,
            "run": _serialize_scan_run(run),
            "breadcrumbs": [
                {"label": "Security posture"},
                {"label": "Coverage", "href": COVERAGE_PATH},
                {"label": "Run history", "href": COVERAGE_RUNS_PATH},
                {"label": f"Run #{run.id}", "current": True},
            ],
        },
    )


@router.get(
    PRODUCT_COVERAGE_PATH,
    response_class=HTMLResponse,
    name="coverage_product_drilldown",
)
async def coverage_product_detail(
    request: Request,
    product_id: int,
    only_problems: bool = False,
    status: str = "all",
    db: Session = Depends(get_db),
):
    status_filter, only_problems = _normalize_coverage_filters(
        status,
        only_problems,
    )

    audit_service = AuditService(db)
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    rows = audit_service.get_latest_rows(only_problems=False, stop_window_days=14)
    attribution = RepositoryAttribution(db)
    repository_target_index = _build_repository_target_index(db)
    product_rows = []
    for row in rows:
        repo = row["repository"]
        repo_host = (repo.instance_host or "").lower()
        repo_path = (repo.path_with_namespace or "").strip("/").lower()
        owner = attribution.product_for(repo.instance_host, repo.path_with_namespace)
        if owner is not None and owner.id == product.id:
            row["target_repository"] = repository_target_index.get(
                (repo_host, repo_path)
            )
            product_rows.append(row)

    counts = _coverage_counts(product_rows)
    filtered = _filter_coverage_rows(
        product_rows,
        status_filter,
        only_problems,
    )
    coverage_filter_active = only_problems or status_filter != "all"
    status_meta = {
        AuditCoverageStatus.COVERED.value: {"label": "Active", "class": "success", "icon": "check-circle"},
        AuditCoverageStatus.PIPELINE_NO_SEC.value: {"label": "Not Configured", "class": "error", "icon": "warning"},
        AuditCoverageStatus.NO_PIPELINES.value: {"label": "Inactive", "class": "unknown", "icon": "clock"},
        AuditCoverageStatus.INACCESSIBLE.value: {"label": "Inaccessible", "class": "cancelled", "icon": "x-circle"},
        AuditCoverageStatus.EXCLUDED.value: {"label": "Excluded", "class": "skipped", "icon": "stop"},
    }

    group_path = _normalize_product_group_path(product.path, product.name)
    group_url = f"{(product.gitlab_base_url or '').rstrip('/')}/{group_path}" if product.gitlab_base_url else None

    return templates.TemplateResponse(
        "audit_product_detail.html",
        {
            "request": request,
            "product": product,
            "group_path": group_path,
            "group_url": group_url,
            "breadcrumbs": [
                {"label": "Security posture"},
                {"label": "Coverage", "href": COVERAGE_PATH},
                {"label": product.name, "current": True},
            ],
            "rows": filtered,
            "counts": counts,
            "coverage_metrics": _coverage_metric_cards(
                counts,
                base_url=product_coverage_url(product.id),
                status_filter=status_filter,
                only_problems=only_problems,
            ),
            "coverage_filter_active": coverage_filter_active,
            "matching_count": len(filtered),
            "status_meta": status_meta,
            "only_problems": only_problems,
            "status_filter": status_filter,
        },
    )


@router.get("/audit", response_class=RedirectResponse)
async def coverage_compatibility(request: Request):
    destination = (
        "/coverage/operator"
        if any(key in request.query_params for key in _AUDIT_OPERATOR_QUERY_KEYS)
        else COVERAGE_PATH
    )
    return RedirectResponse(
        compatibility_redirect_url(request, destination),
        status_code=308,
    )


@router.get("/audit/products/{product_id}", response_class=RedirectResponse)
async def coverage_product_compatibility(request: Request, product_id: int):
    return RedirectResponse(
        compatibility_redirect_url(request, product_coverage_url(product_id)),
        status_code=308,
    )


@router.post("/audit/run")
async def run_audit_scan(
    product_ids: Optional[list[int]] = Form(default=None),
    db: Session = Depends(get_db),
):
    db_service = DatabaseService(db)
    normalized = db_service.normalize_product_paths()
    if normalized:
        logger.info("Audit scan: normalized product paths=%s", normalized)

    selected = product_ids or []
    if not selected:
        selected = [row[0] for row in db.query(Product.id).order_by(Product.id.asc()).all()]

    run_items: List[tuple[int, int]] = []
    total = len(selected)
    for idx, pid in enumerate(selected, start=1):
        run = AuditScanRun(
            status="queued",
            requested_product_ids=str(pid),
            selected_products=1,
            message=f"Queued ({idx}/{total})",
        )
        db.add(run)
        db.flush()
        run_items.append((run.id, pid))
    db.commit()

    if len(run_items) == 1:
        _schedule_audit_scan(run_items[0][0], [run_items[0][1]])
    elif run_items:
        _schedule_audit_scan_batch(run_items)

    selected_qs = ",".join(str(item) for item in selected)
    focus_run_id = run_items[0][0] if run_items else None
    url = "/coverage/operator?scan_started=true"
    if focus_run_id:
        url = f"{url}&run_id={focus_run_id}"
    if selected_qs:
        url = f"/coverage/operator?selected_product_ids={selected_qs}&only_problems=false&scan_started=true"
        if focus_run_id:
            url = f"{url}&run_id={focus_run_id}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/audit/scan-status", response_class=JSONResponse)
async def audit_scan_status(
    run_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(AuditScanRun)
    if run_id:
        run = query.filter(AuditScanRun.id == run_id).first()
    else:
        run = query.order_by(AuditScanRun.id.desc()).first()
    return {"run": _serialize_scan_run(run)}


@router.post("/audit/scan-cancel")
async def audit_scan_cancel(
    run_id: int = Form(...),
    db: Session = Depends(get_db),
):
    run = db.query(AuditScanRun).filter(AuditScanRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Audit run not found")

    task = _audit_task_registry.get(run_id)
    if task and not task.done():
        task.cancel()
    else:
        if run.status in ("queued", "running"):
            run.status = "cancelled"
            run.finished_at = datetime.now(timezone.utc)
            run.message = "Audit scan cancelled (task not active)"
            db.add(run)
            db.commit()

    return RedirectResponse(url="/coverage/operator", status_code=303)
