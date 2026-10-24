"""Web view for async PT AI scans (status read from ASOC by the poller)."""
import math
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from ...alerting.ptai_scan_status import asoc_repository_url, poll_one_scan
from ...database import DatabaseService, ptai_canonical_status_predicates
from ...integration_settings import runtime_setting
from ...models import PtaiScan
from ...ptai_client import ptai_result_url
from ...target_navigation import compatibility_redirect_url
from .common import get_db, templates

router = APIRouter(tags=["web"])

PAGE_SIZE = 100


def _runtime_urls(db: Session):
    session = db if hasattr(db, "query") else None
    return (
        runtime_setting("HEXWAY_URL", "", db=session),
        runtime_setting("PTAI_URL", "", db=session),
        runtime_setting("PTAI_RESULT_URL_TEMPLATE", "", db=session),
    )


def _ptai_result_link(scan, base_url: str, template: str):
    try:
        return ptai_result_url(scan, base_url, template)
    except TypeError:
        # Keep compatibility with tests/integrations that provide the legacy one-argument helper.
        return ptai_result_url(scan)


def _asoc_repository_link(scan, base_url: str):
    try:
        return asoc_repository_url(scan, base_url)
    except TypeError:
        return asoc_repository_url(scan)


def _load_scan(db: Session, scan_id: int) -> PtaiScan:
    scan = (
        db.query(PtaiScan)
        .options(joinedload(PtaiScan.repository), joinedload(PtaiScan.product))
        .filter(PtaiScan.id == scan_id)
        .first()
    )
    if not scan:
        raise HTTPException(status_code=404, detail="PT AI scan not found")
    return scan


def _ptai_summary(db: Session) -> dict:
    predicates = ptai_canonical_status_predicates()
    count_labels = {
        status: func.coalesce(
            func.sum(case((predicate, 1), else_=0)),
            0,
        ).label(status.lower())
        for status, predicate in predicates.items()
    }
    status_counts = db.query(*count_labels.values()).one()
    counts = {
        status: int(getattr(status_counts, label) or 0)
        for status, label in ((status, status.lower()) for status in predicates)
    }
    last_poll = db.query(func.max(PtaiScan.last_polled_at)).scalar()
    return {
        "total": sum(counts.values()),
        "pending": counts.get("PENDING", 0),
        "running": counts.get("IN_PROGRESS", 0),
        "success": counts.get("SUCCESS", 0),
        "problems": counts.get("ERROR", 0) + counts.get("PARTIAL_SUCCESS", 0),
        "last_poll": last_poll,
    }


@router.get("/classic/ptai-scans", response_class=RedirectResponse, include_in_schema=False)
async def ptai_compatibility(request: Request):
    return RedirectResponse(
        compatibility_redirect_url(request, "/ptai-scans"),
        status_code=308,
    )


@router.get(
    "/ptai-scans/operator",
    response_class=HTMLResponse,
    name="ptai_scans_operator",
)
async def ptai_scans(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query("all"),
    repo: str = Query(""),
    branch: str = Query(""),
    problem: bool = Query(False),
    db: Session = Depends(get_db),
):
    db_service = DatabaseService(db)
    rows, total = db_service.list_ptai_scans(
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        status=status,
        repo=repo,
        branch=branch,
        problem_only=problem,
    )
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    filters = {
        "status": (status or "all").strip().upper(),
        "repo": (repo or "").strip(),
        "branch": (branch or "").strip(),
        "problem": problem,
    }
    query_base = urlencode(
        {
            key: value
            for key, value in {
                "status": filters["status"].lower(),
                "repo": filters["repo"],
                "branch": filters["branch"],
                "problem": "true" if problem else "",
            }.items()
            if value
        }
    )
    asoc_base_url, ptai_base_url, ptai_template = _runtime_urls(db)
    return templates.TemplateResponse(
        "ptai_scans.html",
        {
            "request": request,
            "scans": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "filters": filters,
            "query_base": query_base,
            "summary": _ptai_summary(db),
            "asoc_repository_url": lambda scan: _asoc_repository_link(scan, asoc_base_url),
            "ptai_result_url": lambda scan: _ptai_result_link(scan, ptai_base_url, ptai_template),
        },
    )


@router.get("/ptai-scans/{scan_id}", response_class=HTMLResponse)
async def ptai_scan_detail(request: Request, scan_id: int, db: Session = Depends(get_db)):
    scan = _load_scan(db, scan_id)
    asoc_base_url, ptai_base_url, ptai_template = _runtime_urls(db)
    return templates.TemplateResponse(
        "ptai_scan_detail.html",
        {
            "request": request,
            "scan": scan,
            "debug": None,
                "asoc_repository_url": _asoc_repository_link(scan, asoc_base_url),
            "ptai_result_url": _ptai_result_link(scan, ptai_base_url, ptai_template),
        },
    )


@router.post("/ptai-scans/{scan_id}/repoll", response_class=HTMLResponse)
def ptai_scan_repoll(request: Request, scan_id: int, db: Session = Depends(get_db)):
    # Sync def: poll_one_scan does blocking HTTP to ASOC, so FastAPI runs this
    # in the threadpool. Renders the detail page with the raw poll result.
    debug = poll_one_scan(db, scan_id)
    scan = _load_scan(db, scan_id)
    asoc_base_url, ptai_base_url, ptai_template = _runtime_urls(db)
    return templates.TemplateResponse(
        "ptai_scan_detail.html",
        {
            "request": request,
            "scan": scan,
            "debug": debug,
            "asoc_repository_url": _asoc_repository_link(scan, asoc_base_url),
            "ptai_result_url": _ptai_result_link(scan, ptai_base_url, ptai_template),
        },
    )
