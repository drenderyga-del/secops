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
    ConfigScanFinding,
)
from ...database import DatabaseService
from ...audit_service import AuditService
from ...config_scan_service import ConfigScanService, project_counts_for_run
from ...config_scan_reporting import findings_severity_counts, format_scan_duration, gitlab_path_from_product_field
from ...iac_service import IacService
from ...models.iac import IacScanRun
from ...models.job_report import JobStatus
import math

from fastapi import APIRouter
from .common import (
    _format_local_datetime,
    _normalize_product_group_path,
    get_db,
    templates,
)

router = APIRouter(tags=["web"])
logger = logging.getLogger(__name__)

@router.get("/classic/config-scan", response_class=RedirectResponse, include_in_schema=False)
async def config_scan_compatibility(request: Request):
    product_values = request.query_params.getlist("product")
    product_id = None
    if len(product_values) == 1:
        try:
            candidate = int(product_values[0])
        except (TypeError, ValueError):
            candidate = 0
        if candidate > 0:
            product_id = candidate

    destination = (
        f"/config-scan/products/{product_id}"
        if product_id is not None
        else "/config-scan/operator"
    )
    query = urlencode(
        [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key != "product"
        ],
        doseq=True,
    )
    return RedirectResponse(
        f"{destination}?{query}" if query else destination,
        status_code=308,
    )


@router.get(
    "/config-scan/operator",
    response_class=HTMLResponse,
    name="config_scan_operator",
)
async def config_scan_overview(request: Request, db: Session = Depends(get_db)):
    scan_service = ConfigScanService(db)
    products = db.query(Product).order_by(Product.name.asc()).all()
    latest_runs = scan_service.get_latest_run_per_product()
    latest_succeeded = scan_service.get_latest_succeeded_run_per_product()

    active_run = (
        db.query(ConfigScanRun)
        .filter(ConfigScanRun.status.in_(["queued", "running"]))
        .order_by(ConfigScanRun.id.desc())
        .first()
    )
    active_run_ps, active_run_pt = project_counts_for_run(active_run)

    product_scans = []
    for product in products:
        run = latest_runs.get(product.id)
        succeeded_run = latest_succeeded.get(product.id)
        overview = scan_service.get_product_overview_summary(product)
        summary = overview["severity"] if overview["projects_scanned"] else None

        full_scope = gitlab_path_from_product_field(product.path)
        group_path = full_scope if full_scope else _normalize_product_group_path(
            product.path, product.name
        )
        ps_disp = overview["projects_scanned"]
        pt_disp = overview["projects_total"]
        findings_display = overview["findings_total"]

        metrics_hint = None
        if succeeded_run and run and succeeded_run.id != run.id:
            ts = (
                _format_local_datetime(succeeded_run.finished_at, "%Y-%m-%d %H:%M")
                if succeeded_run.finished_at
                else ""
            )
            metrics_hint = (
                f"Aggregated from latest repo scans; last OK run #{succeeded_run.id}"
                + (f" — {ts}" if ts else "")
            )

        product_scans.append({
            "product": product,
            "run": run,
            "summary": summary,
            "group_path": group_path,
            "projects_scanned_display": ps_disp,
            "projects_total_display": pt_disp,
            "findings_display": findings_display,
            "metrics_hint": metrics_hint,
        })

    return templates.TemplateResponse(
        "config_scan.html",
        {
            "request": request,
            "product_scans": product_scans,
            "scan_run": active_run,
            "scan_run_projects_scanned": active_run_ps,
            "scan_run_projects_total": active_run_pt,
        },
    )


@router.get("/config-scan/products/{product_id}", response_class=HTMLResponse)
async def config_scan_product(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    scan_service = ConfigScanService(db)
    runs = scan_service.get_runs_for_product(product_id)
    latest_run = runs[0] if runs else None
    last_succeeded = scan_service.get_last_succeeded_run(product_id)

    severity_summary = scan_service.get_product_findings_summary(product_id)
    summary = scan_service.parse_summary_json(last_succeeded)
    bundle = scan_service.build_product_overview_bundle(product)

    # Product page is an aggregate view across repos; keep a coherent summary block.
    if bundle.get("repo_table_rows"):
        overview = scan_service.summarize_product_overview_bundle(bundle)
        severity_summary = overview["severity"]
        summary = {
            "severity": overview["severity"],
            "projects_scanned": overview["projects_scanned"],
            "projects_with_findings": overview["projects_with_findings"],
        }

    history_rows = []
    for r in runs:
        ps_d, pt_d = project_counts_for_run(r)
        history_rows.append({
            "run": r,
            "duration": format_scan_duration(r.started_at, r.finished_at),
            "projects_scanned_display": ps_d,
            "projects_total_display": pt_d,
        })

    return templates.TemplateResponse(
        "config_scan_product.html",
        {
            "request": request,
            "product": product,
            "runs": runs,
            "history_rows": history_rows,
            "latest_run": latest_run,
            "last_succeeded": last_succeeded,
            "severity_summary": severity_summary,
            "summary": summary,
            **bundle,
        },
    )


@router.get("/config-scan/runs/{run_id}", response_class=HTMLResponse)
async def config_scan_run_detail(
    request: Request,
    run_id: int,
    db: Session = Depends(get_db),
):
    scan_service = ConfigScanService(db)
    run = scan_service.get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Config scan run not found")

    product = db.query(Product).filter(Product.id == run.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    summary = scan_service.parse_summary_json(run)
    bundle = scan_service.build_results_bundle(product, run)
    ps_disp, pt_disp = project_counts_for_run(run)

    return templates.TemplateResponse(
        "config_scan_run.html",
        {
            "request": request,
            "run": run,
            "product": product,
            "projects_scanned_display": ps_disp,
            "projects_total_display": pt_disp,
            "summary": summary,
            **bundle,
        },
    )


@router.get("/config-scan/runs/{run_id}/findings", response_class=HTMLResponse)
async def config_scan_run_project_findings(
    request: Request,
    run_id: int,
    project: str = Query("", description="GitLab path_with_namespace"),
    db: Session = Depends(get_db),
):
    scan_service = ConfigScanService(db)
    run = scan_service.get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Config scan run not found")
    product = db.query(Product).filter(Product.id == run.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    project_query = project.strip().strip("/")
    if project_query:
        blob = scan_service.resolve_project_findings_payload(run_id, project_query)
        if not blob:
            raise HTTPException(
                status_code=404,
                detail="Repository not found in this scan",
            )
        findings = blob.get("findings") or []
        fs = blob.get("findings_summary") or findings_severity_counts(findings)
        project_path = blob.get("project_path") or project_query
        project_url = blob.get("project_url")
    else:
        rows = (
            db.query(ConfigScanFinding)
            .filter(ConfigScanFinding.run_id == run_id)
            .order_by(ConfigScanFinding.project_path.asc(), ConfigScanFinding.id.asc())
            .all()
        )
        findings = [
            {
                "severity": row.severity,
                "code": row.rule_id,
                "rule_id": row.rule_id,
                "title": row.title,
                "description": row.description,
                "recommendation": row.recommendation,
                "evidence": row.detail,
                "detail": row.detail,
                "project_path": row.project_path,
                "project_url": row.project_url,
            }
            for row in rows
        ]
        fs = findings_severity_counts(findings)
        project_path = "All repositories"
        project_url = None

    return templates.TemplateResponse(
        "config_scan_run_findings.html",
        {
            "request": request,
            "run": run,
            "product": product,
            "project_path": project_path,
            "project_url": project_url,
            "findings": findings,
            "findings_summary": fs,
        },
    )


@router.get("/config-scan/runs/{run_id}/report/json")
async def config_scan_download_json(run_id: int, db: Session = Depends(get_db)):
    scan_service = ConfigScanService(db)
    run = scan_service.get_run_by_id(run_id)
    if not run or not run.report_json_path:
        raise HTTPException(status_code=404, detail="JSON report not found")
    path = Path(run.report_json_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="JSON report file is missing")
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.get("/config-scan/runs/{run_id}/report/html")
async def config_scan_download_html(run_id: int, db: Session = Depends(get_db)):
    scan_service = ConfigScanService(db)
    run = scan_service.get_run_by_id(run_id)
    if not run or not run.report_html_path:
        raise HTTPException(status_code=404, detail="HTML report not found")
    path = Path(run.report_html_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="HTML report file is missing")
    return FileResponse(path, media_type="text/html", filename=path.name)


@router.post("/config-scan/products/{product_id}/repositories/{repository_id}/scan")
async def config_scan_one_repository(
    request: Request,
    product_id: int,
    repository_id: int,
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    repo = db.query(Repository).filter(Repository.id == repository_id).first()
    if not repo or repo.product_id != product_id:
        raise HTTPException(status_code=404, detail="Repository not found")

    poller = request.app.state.config_scan_poller
    try:
        run_id = await poller.trigger_repo_scan(product_id, repository_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/config-scan/runs/{run_id}", status_code=303)


@router.post("/config-scan/run")
async def config_scan_trigger(
    request: Request,
    product_ids: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
):
    poller = request.app.state.config_scan_poller

    if product_ids:
        ids = [int(x.strip()) for x in product_ids.split(",") if x.strip().isdigit()]
    else:
        ids = [p.id for p in db.query(Product.id).order_by(Product.id.asc()).all()]

    if ids:
        await poller.trigger_manual_scan(ids)

    return RedirectResponse(url="/config-scan/operator", status_code=303)


@router.get("/config-scan/scan-status", response_class=JSONResponse)
async def config_scan_status(
    run_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    scan_service = ConfigScanService(db)
    if run_id:
        run = scan_service.get_run_by_id(run_id)
    else:
        run = db.query(ConfigScanRun).order_by(ConfigScanRun.id.desc()).first()

    if not run:
        return {"run": None}

    sc_disp, tot_disp = project_counts_for_run(run)
    return {
        "run": {
            "id": run.id,
            "product_id": run.product_id,
            "status": run.status,
            "trigger": run.trigger,
            "total_projects": tot_disp,
            "scanned_projects": sc_disp,
            "findings_count": run.findings_count,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
    }


@router.post("/config-scan/scan-target")
async def config_scan_target(
    request: Request,
    target_url: str = Form(...),
    db: Session = Depends(get_db),
):
    target_url = target_url.strip().rstrip("/")
    if not target_url:
        return RedirectResponse(url="/config-scan/operator", status_code=303)

    parsed = urlparse(target_url)
    if not parsed.scheme or not parsed.netloc:
        return RedirectResponse(url="/config-scan/operator", status_code=303)

    gitlab_base_url = f"{parsed.scheme}://{parsed.netloc}"
    scan_path = (parsed.path or "").strip("/")
    if not scan_path:
        return RedirectResponse(url="/config-scan/operator", status_code=303)

    segments = [s for s in scan_path.split("/") if s]
    if not segments:
        return RedirectResponse(url="/config-scan/operator", status_code=303)

    db_service = DatabaseService(db)
    best_product, product_created = db_service.get_or_create_product_with_meta(
        {
            "url": gitlab_base_url,
            "project": {"path": scan_path},
        }
    )
    if product_created:
        logger.info(
            "Created product '%s' path=%s (id=%s) from scan target URL",
            best_product.name,
            best_product.path,
            best_product.id,
        )

    poller = request.app.state.config_scan_poller
    run_id = await poller.trigger_scan_by_url(gitlab_base_url, scan_path, best_product.id)

    return RedirectResponse(url=f"/config-scan/runs/{run_id}", status_code=303)
