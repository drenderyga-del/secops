from fastapi import APIRouter, Depends, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from typing import Optional, Iterable, Set, List, Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from uuid import uuid4
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
from ...iac_asoc import AsocClient
from ...iac_service import IacService
from ...models.iac import IacScanFinding, IacScanRepository, IacScanRun
from ...models.job_report import JobStatus
from ...target_navigation import compatibility_redirect_url
import math

from fastapi import APIRouter
from .common import (
    _parse_gitlab_target_url,
    get_db,
    templates,
)

router = APIRouter(tags=["web"])
logger = logging.getLogger(__name__)

@router.get("/classic/iac", response_class=RedirectResponse, include_in_schema=False)
async def iac_compatibility(request: Request):
    return RedirectResponse(
        compatibility_redirect_url(request, "/iac/operator"),
        status_code=308,
    )


@router.get(
    "/iac/operator",
    response_class=HTMLResponse,
    name="iac_operator",
)
async def iac_dashboard(request: Request, scan_error: str = Query(""), db: Session = Depends(get_db)):
    service = IacService(db)
    dashboard_data = service.dashboard()
    latest_repos, latest_total = service.latest_repo_rows(limit=25)
    active_run = (
        db.query(IacScanRun)
        .filter(IacScanRun.status.in_(["queued", "running"]))
        .order_by(IacScanRun.id.desc())
        .first()
    )
    return templates.TemplateResponse(
        "iac.html",
        {
            "request": request,
            "dashboard": dashboard_data,
            "latest_repos": latest_repos,
            "latest_total": latest_total,
            "active_run": active_run,
            "scan_error": scan_error,
            "active_tab": "dashboard",
        },
    )


@router.post("/iac/scan-target")
async def iac_scan_target(
    target_url: str = Form(...),
    db: Session = Depends(get_db),
):
    parsed = _parse_gitlab_target_url(target_url)
    if not parsed:
        # Bad URL or a GitLab host we have no token for — say so instead of
        # silently dropping the submit.
        return RedirectResponse(
            url="/iac/operator?scan_error=invalid_target",
            status_code=303,
        )
    gitlab_base_url, scope_path = parsed
    run = IacService(db).create_run(
        target_url=target_url.strip().rstrip("/"),
        gitlab_base_url=gitlab_base_url,
        scope_path=scope_path,
    )
    return RedirectResponse(url=f"/iac/runs/{run.id}", status_code=303)


@router.get("/iac/runs", response_class=HTMLResponse)
async def iac_runs(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "iac_runs.html",
        {
            "request": request,
            "runs": IacService(db).list_runs(limit=100),
            "active_tab": "runs",
        },
    )


@router.get("/iac/runs/{run_id}", response_class=HTMLResponse)
async def iac_run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    service = IacService(db)
    run = service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="IaC run not found")
    repos = service.run_repositories(run_id)
    return templates.TemplateResponse(
        "iac_run.html",
        {
            "request": request,
            "run": run,
            "repos": repos,
            "active_tab": "runs",
        },
    )


@router.post("/iac/runs/{run_id}/cancel")
def iac_run_cancel(run_id: int, db: Session = Depends(get_db)):
    service = IacService(db)
    try:
        run = service.cancel_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not run:
        raise HTTPException(status_code=404, detail="IaC run not found")
    return RedirectResponse(url=f"/iac/runs/{run_id}", status_code=303)


@router.get("/iac/repositories", response_class=HTMLResponse)
async def iac_repositories(
    request: Request,
    page: int = Query(1, ge=1),
    q: str = Query(""),
    iac: str = Query("all"),
    scan_status: str = Query(""),
    asoc_status: str = Query(""),
    has_important_findings: str = Query(""),
    bulk_message: str = Query(""),
    db: Session = Depends(get_db),
):
    page_size = 100
    service = IacService(db)
    if iac not in {"all", "yes", "no"}:
        iac = "all"
    service_filters = {
        "query_text": q,
        "iac": iac,
        "scan_status": scan_status,
        "asoc_status": asoc_status,
        "has_important_findings": has_important_findings == "yes",
    }
    url_filters = {
        "q": q,
        "iac": iac,
        "scan_status": scan_status,
        "asoc_status": asoc_status,
        "has_important_findings": has_important_findings == "yes",
    }
    repos, total = service.latest_repo_rows(
        limit=page_size,
        offset=(page - 1) * page_size,
        **service_filters,
    )
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages
        repos, total = service.latest_repo_rows(
            limit=page_size,
            offset=(page - 1) * page_size,
            **service_filters,
        )
    filter_values = service.repository_filter_values()
    return templates.TemplateResponse(
        "iac_repositories.html",
        {
            "request": request,
            "repos": repos,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "iac": iac,
            "scan_status": scan_status,
            "asoc_status": asoc_status,
            "has_important_findings": has_important_findings == "yes",
            "scan_statuses": filter_values["scan_statuses"],
            "asoc_statuses": filter_values["asoc_statuses"],
            "bulk_message": bulk_message,
            "reupload_request_key": str(uuid4()),
            "return_to": _iac_repositories_url(page=page, **url_filters),
            "previous_url": _iac_repositories_url(page=page - 1, **url_filters) if page > 1 else None,
            "next_url": _iac_repositories_url(page=page + 1, **url_filters) if page < total_pages else None,
            "active_tab": "repositories",
        },
    )


@router.post("/iac/repositories/{repo_row_id}/rescan")
async def iac_repository_rescan(repo_row_id: int, db: Session = Depends(get_db)):
    service = IacService(db)
    repo = service.get_repo_row(repo_row_id)
    if not repo:
        raise HTTPException(status_code=404, detail="IaC repository scan not found")
    run = service.create_single_repo_run(repo)
    return RedirectResponse(url=f"/iac/runs/{run.id}", status_code=303)


@router.post("/iac/repositories/{repo_row_id}/reupload-asoc")
def iac_repository_reupload_asoc(
    repo_row_id: int,
    request_key: str = Form(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$"),
    db: Session = Depends(get_db),
):
    """Re-push the existing SARIF into ASOC without rescanning.

    Useful after the ASOC card appears (or matching improves) for repos whose
    upload was skipped: the SARIF is already on the shared reports volume.
    Sync def on purpose — the ASOC client is blocking requests, FastAPI runs
    this in its threadpool.
    """
    service = IacService(db)
    row = service.get_repo_row(repo_row_id)
    if not row:
        raise HTTPException(status_code=404, detail="IaC repository scan not found")
    if not row.sarif_path:
        raise HTTPException(status_code=400, detail="No SARIF for this scan — run a rescan instead")
    run = service.get_run(row.run_id)
    gitlab_base_url = (run.gitlab_base_url if run else "") or _gitlab_base_from_url(row.project_url)
    attempt_key = f"manual:{request_key}:{row.id}"
    if not service.prepare_delivery_attempt(row, delivery_attempt_key=attempt_key):
        return RedirectResponse(url=f"/iac/repositories/{repo_row_id}", status_code=303)
    db.commit()
    try:
        upload = AsocClient().upload_repo_row(row, gitlab_base_url)
    except Exception as exc:
        row.asoc_upload_status = "failed"
        row.error_text = f"ASOC re-upload failed: {exc}"
        service.sync_repository_execution(row, delivery_attempt_key=attempt_key)
        db.commit()
        logger.exception("iac_asoc_reupload_failed repo_row_id=%s", repo_row_id)
        return RedirectResponse(url=f"/iac/repositories/{repo_row_id}", status_code=303)
    service.sync_repository_execution(row, delivery_attempt_key=attempt_key)
    db.commit()
    service.refresh_run_counters(row.run_id)
    logger.info(
        "iac_asoc_reupload repo_row_id=%s project=%s status=%s slug=%s matched_by=%s message=%s",
        repo_row_id,
        row.project_path,
        upload.status,
        row.asoc_repository_slug,
        upload.matched_by,
        upload.message,
    )
    return RedirectResponse(url=f"/iac/repositories/{repo_row_id}", status_code=303)


@router.post("/iac/repositories/reupload-asoc")
def iac_repositories_reupload_asoc(
    repo_row_ids: List[int] = Form([]),
    return_to: str = Form("/iac/repositories"),
    request_key: str = Form(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$"),
    db: Session = Depends(get_db),
):
    """Re-upload selected existing SARIF reports without queueing rescans."""
    outcome = _bulk_reupload_asoc_rows(IacService(db), repo_row_ids, request_key=request_key)
    message = (
        f"ASOC re-upload: {outcome['uploaded']} uploaded, "
        f"{outcome['skipped']} skipped, {outcome['failed']} failed."
    )
    return RedirectResponse(
        url=_append_bulk_message(_safe_iac_repositories_return_to(return_to), message),
        status_code=303,
    )


@router.post("/iac/repositories/reupload-skipped-asoc")
def iac_repositories_reupload_skipped_asoc(
    return_to: str = Form("/iac/repositories"),
    request_key: str = Form(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$"),
    q: str = Form(""),
    iac: str = Form("all"),
    scan_status: str = Form(""),
    has_important_findings: str = Form(""),
    db: Session = Depends(get_db),
):
    """Re-upload every eligible skipped row from the currently filtered view."""
    service = IacService(db)
    rows, _ = service.latest_repo_rows(
        limit=10_000,
        query_text=q,
        iac=iac if iac in {"all", "yes", "no"} else "all",
        scan_status=scan_status,
        asoc_status="skipped",
        has_important_findings=has_important_findings == "yes",
    )
    outcome = _bulk_reupload_asoc_rows(
        service,
        [row.id for row in rows if row.sarif_path],
        request_key=request_key,
    )
    message = f"ASOC re-upload all skipped: {outcome['uploaded']} uploaded, {outcome['skipped']} skipped, {outcome['failed']} failed."
    return RedirectResponse(url=_append_bulk_message(_safe_iac_repositories_return_to(return_to), message), status_code=303)


def _gitlab_base_from_url(project_url: str | None) -> str:
    parsed = urlparse(project_url or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _bulk_reupload_asoc_rows(
    service: IacService,
    repo_row_ids: Iterable[int],
    *,
    request_key: str,
) -> dict[str, int]:
    """Upload every valid selected row; one ASOC error never stops the batch."""
    client = AsocClient()
    outcome = {"uploaded": 0, "skipped": 0, "failed": 0}
    affected_run_ids: set[int] = set()
    seen_ids: set[int] = set()
    for repo_row_id in repo_row_ids:
        if repo_row_id in seen_ids:
            continue
        seen_ids.add(repo_row_id)
        row = service.get_repo_row(repo_row_id)
        if not row or not row.sarif_path:
            outcome["skipped"] += 1
            continue
        attempt_key = f"manual:{request_key}:{row.id}"
        if not service.prepare_delivery_attempt(row, delivery_attempt_key=attempt_key):
            outcome["uploaded" if row.asoc_upload_status == "uploaded" else "skipped"] += 1
            continue
        service.db.commit()
        try:
            run = service.get_run(row.run_id)
            gitlab_base_url = (run.gitlab_base_url if run else "") or _gitlab_base_from_url(row.project_url)
            upload = client.upload_repo_row(row, gitlab_base_url)
            service.sync_repository_execution(row, delivery_attempt_key=attempt_key)
            service.db.commit()
            affected_run_ids.add(row.run_id)
            if upload.status == "uploaded":
                outcome["uploaded"] += 1
            elif upload.status == "skipped":
                outcome["skipped"] += 1
            else:
                outcome["failed"] += 1
        except Exception as exc:
            service.db.rollback()
            row.asoc_upload_status = "failed"
            row.error_text = f"Bulk ASOC re-upload failed: {exc}"
            service.sync_repository_execution(row, delivery_attempt_key=attempt_key)
            service.db.commit()
            affected_run_ids.add(row.run_id)
            outcome["failed"] += 1
            logger.exception("iac_asoc_bulk_reupload_failed repo_row_id=%s", repo_row_id)
    for run_id in affected_run_ids:
        service.refresh_run_counters(run_id)
    return outcome


def _iac_repositories_url(
    *,
    page: int,
    q: str = "",
    iac: str = "all",
    scan_status: str = "",
    asoc_status: str = "",
    has_important_findings: bool = False,
) -> str:
    params: list[tuple[str, str]] = []
    if page > 1:
        params.append(("page", str(page)))
    if q:
        params.append(("q", q))
    # Keep the selected filter addressable even for the default option.  This
    # makes copied URLs and browser back/forward state describe the complete
    # filter set consistently with the other IaC filters.
    params.append(("iac", iac or "all"))
    if scan_status:
        params.append(("scan_status", scan_status))
    if asoc_status:
        params.append(("asoc_status", asoc_status))
    if has_important_findings:
        params.append(("has_important_findings", "yes"))
    return "/iac/repositories" + (f"?{urlencode(params)}" if params else "")


def _safe_iac_repositories_return_to(return_to: str) -> str:
    """Only redirect a bulk form back to the filtered repository page."""
    parsed = urlsplit(return_to or "")
    if parsed.scheme or parsed.netloc or parsed.path != "/iac/repositories":
        return "/iac/repositories"
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _append_bulk_message(return_to: str, message: str) -> str:
    parsed = urlsplit(return_to)
    params = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "bulk_message"]
    params.append(("bulk_message", message))
    return urlunsplit(("", "", parsed.path, urlencode(params), ""))


@router.get("/iac/repositories/{repo_row_id}/sarif")
async def iac_repository_sarif(repo_row_id: int, db: Session = Depends(get_db)):
    import os

    repo = IacService(db).get_repo_row(repo_row_id)
    if not repo or not repo.sarif_path:
        raise HTTPException(status_code=404, detail="No SARIF for this scan")
    reports_dir = Path(os.getenv("IAC_REPORTS_DIR", "./iac_reports")).resolve()
    target = Path(repo.sarif_path).resolve()
    # Guard against path traversal: the file must live inside the reports dir.
    if not str(target).startswith(str(reports_dir) + os.sep) or not target.is_file():
        raise HTTPException(status_code=404, detail="SARIF file not found on disk")
    return FileResponse(str(target), media_type="application/json", filename=target.name)


@router.get("/iac/repositories/{repo_row_id}", response_class=HTMLResponse)
async def iac_repository_detail(
    request: Request,
    repo_row_id: int,
    scan_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    service = IacService(db)
    repository_target = service.get_repo_row(repo_row_id)
    if not repository_target:
        raise HTTPException(status_code=404, detail="IaC repository scan not found")
    history = service.repository_scan_history(repository_target)
    selected_repo = repository_target
    if scan_id is not None:
        selected_repo = next((row for row in history if row.id == scan_id), None)
        if selected_repo is None:
            raise HTTPException(status_code=404, detail="IaC scan does not belong to this repository")
    run = selected_repo.run or service.get_run(selected_repo.run_id)
    findings = service.findings_for_repo_row(selected_repo.id)
    return templates.TemplateResponse(
        "iac_repository_detail.html",
        {
            "request": request,
            "repo": selected_repo,
            "repository_target": repository_target,
            "run": run,
            "history": history,
            "findings": findings,
            "reupload_request_key": str(uuid4()),
            "active_tab": "repositories",
        },
    )


@router.get("/iac/findings", response_class=HTMLResponse)
async def iac_findings(
    request: Request,
    severity: str = Query(""),
    rule_id: str = Query(""),
    title: str = Query(""),
    rule: str = Query(""),
    project: str = Query(""),
    scanner: str = Query(""),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    service = IacService(db)
    page_size = 25
    findings, total = service.latest_findings_page(
        severity=severity,
        rule_id=rule_id,
        title=title,
        rule_query=rule,
        project_query=project,
        scanner_name=scanner,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages
        findings, total = service.latest_findings_page(
            severity=severity,
            rule_id=rule_id,
            title=title,
            rule_query=rule,
            project_query=project,
            scanner_name=scanner,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
    query_params = [(key, value) for key, value in (("severity", severity), ("rule_id", rule_id), ("title", title), ("rule", rule), ("project", project), ("scanner", scanner)) if value]
    query_suffix = f"&{urlencode(query_params)}" if query_params else ""
    if severity and rule_id and title:
        page_title = rule_id
        page_description = title
        badge_text = severity.upper()
    else:
        page_title = "IaC Findings"
        page_description = "Latest findings across scanned IaC repositories."
        badge_text = "ALL"
    return templates.TemplateResponse(
        "iac_findings.html",
        {
            "request": request,
            "severity": severity,
            "rule_id": rule_id,
            "title": title,
            "page_title": page_title,
            "page_description": page_description,
            "badge_text": badge_text,
            "findings": findings,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "query_suffix": query_suffix,
            "rule": rule,
            "project": project,
            "scanner": scanner,
            "active_tab": "findings",
        },
    )


@router.get("/iac/scan-status", response_class=JSONResponse)
async def iac_scan_status(run_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    query = db.query(IacScanRun)
    if run_id:
        run = query.filter(IacScanRun.id == run_id).first()
    else:
        run = query.order_by(IacScanRun.id.desc()).first()
    if not run:
        return {"run": None}
    return {
        "run": {
            "id": run.id,
            "status": run.status,
            "total_repositories": run.total_repositories,
            "candidate_repositories": run.candidate_repositories,
            "scanned_repositories": run.scanned_repositories,
            "uploaded_repositories": run.uploaded_repositories,
            "findings_count": run.findings_count,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error_text": run.error_text,
        }
    }


# ── Config Scan routes ─────────────────────────────────────────────
