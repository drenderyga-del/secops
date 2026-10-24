from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..models import (
    ActivePipeline,
    PipelineStatus,
    RepositoryControlBinding,
    SecurityControl,
    SessionLocal,
    Repository as RepositoryModel,
)
from ..control_coverage import BINDING_STATUSES, coverage_by_repository
from ..connector_registry import refresh_registry
from ..execution_service import ExecutionService
from ..execution_event_service import EventConflictError, EventProjectionError, ExecutionEventService
from ..database import DatabaseService
from ..audit_service import AuditService
from ..config_scan_service import ConfigScanService, project_counts_for_run
from ..schemas import (
    JobIngestRequest,
    ExecutionEventIngestRequest,
    PipelineRegisterRequest,
    RepositoryControlBindingUpsertRequest,
    SecurityControlUpsertRequest,
)
import os
import secrets
import asyncio
import logging
import time

router = APIRouter(prefix="/api", tags=["api"])
logger = logging.getLogger(__name__)
_PROCESS_STARTED_MONOTONIC = time.monotonic()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/registry")
async def connector_registry(db: Session = Depends(get_db)):
    """Read-only connector and scanner-instance registry; values are redacted."""
    return refresh_registry(db)

def verify_bearer_token(authorization: str = Header(None)):
    """Verify Bearer token for API authentication"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    token = authorization.removeprefix("Bearer ")
    expected_token = os.getenv("PIPE_BEARER_TOKEN")

    if not expected_token:
        raise HTTPException(status_code=500, detail="Server configuration error")

    if not secrets.compare_digest(token.encode(), expected_token.encode()):
        raise HTTPException(status_code=401, detail="Invalid token")

    return token


def _schedule_product_audit_scan(product_id: int):
    async def _run():
        try:
            db = SessionLocal()
            try:
                service = AuditService(db)
                await service.run_scan(product_ids=[product_id])
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Background audit scan failed for product %s: %s", product_id, exc)

    asyncio.create_task(_run())


def _normalize_control_key(value: str) -> str:
    return "-".join((value or "").strip().lower().split())


@router.post("/v1/execution-events")
def ingest_execution_event(
    payload: ExecutionEventIngestRequest,
    db: Session = Depends(get_db),
    token: str = Depends(verify_bearer_token),
):
    """Accept one immutable, idempotent adapter telemetry event."""
    try:
        event, replayed = ExecutionEventService(db).ingest(payload)
        db.commit()
        db.refresh(event)
    except EventConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EventProjectionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = ExecutionEventService.serialize(event)
    body["replayed"] = replayed
    return JSONResponse(status_code=200 if replayed else 201, content=body)


@router.get("/v1/execution-events")
def execution_event_history(
    source: str | None = Query(default=None, max_length=64),
    source_scope: str | None = Query(default=None, max_length=255),
    correlation_key: str | None = Query(default=None, max_length=255),
    execution_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Return accepted telemetry ordered by source observation time."""
    return {
        "events": ExecutionEventService(db).history(
            source=source,
            source_scope=source_scope,
            correlation_key=correlation_key,
            execution_id=execution_id,
            limit=limit,
        )
    }


@router.post("/security-controls", status_code=status.HTTP_201_CREATED)
async def upsert_security_control(
    payload: SecurityControlUpsertRequest,
    db: Session = Depends(get_db),
    token: str = Depends(verify_bearer_token),
):
    """Create or update a scanner-neutral control in the shared vocabulary."""
    control_key = _normalize_control_key(payload.control_key)
    if not control_key or len(control_key) > 128:
        raise HTTPException(status_code=422, detail="control_key must be 1–128 characters")
    control = db.query(SecurityControl).filter(SecurityControl.control_key == control_key).first()
    if control is None:
        control = SecurityControl(control_key=control_key, name=payload.name.strip())
    control.name = payload.name.strip()
    control.category = (payload.category or "").strip() or None
    control.description = (payload.description or "").strip() or None
    control.is_active = payload.is_active
    db.add(control)
    db.commit()
    db.refresh(control)
    return {
        "id": control.id,
        "control_key": control.control_key,
        "name": control.name,
        "category": control.category,
        "is_active": control.is_active,
    }


@router.get("/repositories/{repository_id}/controls")
async def repository_controls(repository_id: int, db: Session = Depends(get_db)):
    """Expose expected-versus-configured coverage and diagnostic binding context."""
    repository = db.query(RepositoryModel).filter(RepositoryModel.id == repository_id).first()
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    bindings = (
        db.query(RepositoryControlBinding)
        .join(SecurityControl)
        .filter(RepositoryControlBinding.repository_id == repository_id)
        .order_by(SecurityControl.control_key.asc(), RepositoryControlBinding.scope.asc())
        .all()
    )
    coverage = coverage_by_repository(db, [repository_id]).get(
        repository_id, {"expected": 0, "configured": 0, "missing": 0, "excluded": 0}
    )
    return {
        "repository_id": repository.id,
        "repository_path": repository.path,
        "coverage": coverage,
        "bindings": [
            {
                "control_key": binding.security_control.control_key,
                "control_name": binding.security_control.name,
                "scope": binding.scope,
                "status": binding.status,
                "source": binding.source,
                "evidence": binding.evidence,
                "reason": binding.reason,
                "last_verified_at": binding.last_verified_at.isoformat() if binding.last_verified_at else None,
            }
            for binding in bindings
        ],
    }


@router.get("/repositories/{repository_id}/executions")
async def repository_executions(
    repository_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Return the scanner-neutral execution timeline for one repository."""
    repository = db.query(RepositoryModel).filter(RepositoryModel.id == repository_id).first()
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    return {
        "repository_id": repository.id,
        "repository_path": repository.path,
        "executions": ExecutionService(db).repository_timeline(repository_id, limit=limit),
    }


@router.put("/repositories/{repository_id}/controls/{control_key}")
async def upsert_repository_control_binding(
    repository_id: int,
    control_key: str,
    payload: RepositoryControlBindingUpsertRequest,
    db: Session = Depends(get_db),
    token: str = Depends(verify_bearer_token),
):
    """Set the observed configuration state for an expected repository control."""
    status_value = (payload.status or "").strip().lower()
    if status_value not in BINDING_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(sorted(BINDING_STATUSES))}")
    repository = db.query(RepositoryModel).filter(RepositoryModel.id == repository_id).first()
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    control = (
        db.query(SecurityControl)
        .filter(SecurityControl.control_key == _normalize_control_key(control_key))
        .first()
    )
    if not control:
        raise HTTPException(status_code=404, detail="Security control not found")
    scope = (payload.scope or "*").strip() or "*"
    binding = (
        db.query(RepositoryControlBinding)
        .filter(
            RepositoryControlBinding.repository_id == repository_id,
            RepositoryControlBinding.security_control_id == control.id,
            RepositoryControlBinding.scope == scope,
        )
        .first()
    )
    if binding is None:
        binding = RepositoryControlBinding(
            repository_id=repository_id,
            security_control_id=control.id,
            scope=scope,
        )
    binding.status = status_value
    binding.source = (payload.source or "").strip() or None
    binding.evidence = (payload.evidence or "").strip() or None
    binding.reason = (payload.reason or "").strip() or None
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return {"repository_id": repository_id, "control_key": control.control_key, "scope": binding.scope, "status": binding.status}

@router.post("/ingest/job")
async def ingest_job(
    request: JobIngestRequest,
    db: Session = Depends(get_db),
    token: str = Depends(verify_bearer_token)
):
    """
    Ingest job data from GitLab CI/CD pipeline
    """
    try:
        db_service = DatabaseService(db)

        # Get or create product
        product, product_created = db_service.get_or_create_product_with_meta(request.gitlab)

        # Get or create repository
        repository = db_service.get_or_create_repository(product, request.gitlab)

        # Create job report
        job_report = db_service.create_job_report(repository, request)

        if product_created:
            _schedule_product_audit_scan(product.id)

        return {
            "status": "success",
            "message": "Job data ingested successfully",
            "job_report_id": job_report.id,
            "repository_id": repository.id,
            "product_id": product.id
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to ingest job data: {str(e)}")


@router.get("/failed_jobs")
async def failed_jobs(
    repo: Optional[str] = Query(None, description="Repository name filter"),
    job: Optional[str] = Query(None, description="Job name filter"),
    category: Optional[str] = Query(None, description="Job category"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=500, description="Items per page"),
    timeframe: Optional[str] = Query("7d", description="Time window (today,3d,7d,30d,all)"),
    db: Session = Depends(get_db)
):
    """Return paginated list of recent failed jobs."""
    try:
        db_service = DatabaseService(db)
        cat_filter = None if not category or category.lower() == "all" else category.lower()
        jobs, total = db_service.get_jobs_paginated(
            repo_filter=repo,
            job_filter=job,
            category_filter=cat_filter,
            status_filter=None,
            page=page,
            page_size=page_size,
            only_failed=True,
            timeframe=timeframe
        )

        items = []
        for j in jobs:
            items.append({
                "id": j.id,
                "repository": j.repository.path if getattr(j, "repository", None) else None,
                "job_name": j.job_name,
                "job_status": j.job_status.label if j.job_status else None,
                "job_status_code": int(j.job_status) if j.job_status else None,
                "duration_seconds": j.duration_seconds,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "pipeline_url": j.pipeline_url,
                "ref": j.ref,
                "created_at": j.created_at.isoformat() if j.created_at else None
            })

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch failed jobs: {str(e)}")


@router.get("/failed_job_filters")
async def failed_job_filters(
    category: Optional[str] = Query(None, description="Job category"),
    only_failed: bool = Query(True, description="Return only failed jobs"),
    timeframe: Optional[str] = Query("7d", description="Time window (today,3d,7d,30d,all)"),
    db: Session = Depends(get_db)
):
    """Return lists of repository names and job names (autocomplete)."""
    try:
        db_service = DatabaseService(db)
        cat_filter = None if not category or category.lower() == "all" else category.lower()
        repo_names, job_names = db_service.get_job_filters(
            category_filter=cat_filter,
            only_failed=only_failed,
            timeframe=timeframe
        )
        return {"repos": repo_names, "jobs": job_names}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch filters: {str(e)}")


@router.post("/pipelines/register")
async def register_pipeline(
    http_request: Request,
    payload: PipelineRegisterRequest,
    db: Session = Depends(get_db),
    token: str = Depends(verify_bearer_token),
):
    """Register a security pipeline so the background poller can monitor it."""
    try:
        db_service = DatabaseService(db)
        product, product_created = db_service.get_or_create_product_with_meta(
            {
                "url": str(payload.project_url) if payload.project_url else "",
                "project": {"path": payload.project_path},
            }
        )
        if product_created:
            _schedule_product_audit_scan(product.id)
        pipeline, pipeline_created = db_service.register_pipeline(payload)
        poller = http_request.app.state.config_scan_poller
        if pipeline_created and poller.is_enabled():
            poller.schedule_pipeline_repo_scan(pipeline.id)
        return {
            "pipeline_id": pipeline.pipeline_id,
            "project_id": pipeline.project_id,
            "status": pipeline.status.value,
            "tracked_jobs": pipeline.tracked_jobs or [],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to register pipeline: {str(e)}")


@router.get("/health")
async def health_check():
    """Health check: DB connectivity + worker heartbeat freshness.

    Returns 503 when the database is unreachable. Stale workers do not fail
    the endpoint (this healthcheck gates the web container, not the workers)
    but are flagged so operators see them in one place.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text as sa_text

    from ..models import WorkerHeartbeat, engine

    payload = {
        "status": "healthy",
        "operational_status": "healthy",
        "service": "security-dashboard",
        "uptime_seconds": int(time.monotonic() - _PROCESS_STARTED_MONOTONIC),
        "last_pipeline_poll_at": None,
        "workers": {},
        "degraded_reasons": [],
        "ui_auth_configured": bool(os.getenv("UI_AUTH_USERNAME") and os.getenv("UI_AUTH_PASSWORD")),
        "credential_store_configured": bool(os.getenv("INTEGRATION_CREDENTIALS_KEY")),
    }
    stale_after = int(os.getenv("HEALTH_WORKER_STALE_SECONDS", "900"))
    poll_stale_after = int(
        os.getenv(
            "HEALTH_PIPELINE_POLL_STALE_SECONDS",
            str(max(900, int(os.getenv("PIPELINE_POLL_INTERVAL_SECONDS", "300")) * 3)),
        )
    )
    expected_worker_setting = os.getenv(
        "HEALTH_EXPECTED_WORKERS", "pipeline_worker,iac_worker"
    ).strip()
    expected_workers = (
        set()
        if expected_worker_setting.lower() in {"", "none"}
        else {
            name.strip()
            for name in expected_worker_setting.split(",")
            if name.strip()
        }
    )
    try:
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            last_pipeline_poll_at = db.query(func.max(ActivePipeline.last_polled_at)).scalar()
            if last_pipeline_poll_at is not None and last_pipeline_poll_at.tzinfo is None:
                last_pipeline_poll_at = last_pipeline_poll_at.replace(tzinfo=timezone.utc)
            payload["last_pipeline_poll_at"] = (
                last_pipeline_poll_at.isoformat() if last_pipeline_poll_at else None
            )
            active_pipeline_count = (
                db.query(ActivePipeline)
                .filter(ActivePipeline.status.in_([PipelineStatus.REGISTERED, PipelineStatus.POLLING]))
                .count()
            )
            if active_pipeline_count and "pipeline_worker" in expected_workers:
                poll_age = (now - last_pipeline_poll_at).total_seconds() if last_pipeline_poll_at else None
                if poll_age is None or poll_age > poll_stale_after:
                    payload["degraded_reasons"].append(
                        f"pipeline polling is stale for {active_pipeline_count} active pipeline(s)"
                    )
            for row in db.query(WorkerHeartbeat).all():
                beat_at = row.last_beat_at
                if beat_at is not None and beat_at.tzinfo is None:
                    beat_at = beat_at.replace(tzinfo=timezone.utc)
                age = (now - beat_at).total_seconds() if beat_at else None
                payload["workers"][row.name] = {
                    "last_beat_at": beat_at.isoformat() if beat_at else None,
                    "stale": age is None or age > stale_after,
                }
            missing_workers = sorted(expected_workers - set(payload["workers"]))
            stale_workers = sorted(
                name
                for name in expected_workers
                if payload["workers"].get(name, {}).get("stale")
            )
            if missing_workers:
                payload["degraded_reasons"].append(
                    f"worker heartbeat missing: {', '.join(missing_workers)}"
                )
            if stale_workers:
                payload["degraded_reasons"].append(
                    f"worker heartbeat stale: {', '.join(stale_workers)}"
                )
            from ..agent_triage import agent_credential_conflict
            if agent_credential_conflict():
                payload["degraded_reasons"].append("authentication credentials conflict")
            if not payload["ui_auth_configured"]:
                payload["degraded_reasons"].append("UI authentication is not configured")
            if not payload["credential_store_configured"]:
                payload["degraded_reasons"].append("integration credential store is not configured")
            if payload["degraded_reasons"]:
                payload["operational_status"] = "degraded"
        finally:
            db.close()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "service": "security-dashboard", "detail": "database unreachable"},
        )
    return payload


@router.post("/audit/scan")
async def audit_scan(
    product_ids: Optional[str] = Query(None, description="Comma-separated product IDs"),
    db: Session = Depends(get_db),
    token: str = Depends(verify_bearer_token),
):
    """Run full audit scan for configured GitLab instances."""
    try:
        service = AuditService(db)
        selected_ids = [
            int(item.strip())
            for item in (product_ids or "").split(",")
            if item.strip().isdigit()
        ]
        result = await service.run_scan(product_ids=selected_ids)
        return {"status": "success", "result": result, "selected_product_ids": selected_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run audit scan: {str(e)}")


@router.get("/audit/summary")
async def audit_summary(db: Session = Depends(get_db)):
    """Return latest audit summary and rows with problems."""
    try:
        service = AuditService(db)
        summary = service.get_latest_summary()
        lifecycle = service.get_latest_lifecycle_summary(stop_window_days=14)
        trend = service.get_scan_trend(limit=10)
        rows = service.get_latest_rows(only_problems=True, stop_window_days=14)
        return {
            "summary": summary,
            "lifecycle": lifecycle,
            "trend": [
                {
                    "scanned_at": point["scanned_at"].isoformat() if point["scanned_at"] else None,
                    "total": point["total"],
                    "covered": point["covered"],
                    "problem": point["problem"],
                }
                for point in trend
            ],
            "problems": [
                {
                    "instance_host": row["repository"].instance_host,
                    "project_id": row["repository"].project_id,
                    "path_with_namespace": row["repository"].path_with_namespace,
                    "web_url": row["repository"].web_url,
                    "status": row["snapshot"].status.value,
                    "reason": row["snapshot"].reason,
                    "pipeline_count": row["snapshot"].pipeline_count,
                    "security_job_count": row["snapshot"].security_job_count,
                    "last_pipeline_at": row["snapshot"].last_pipeline_at.isoformat() if row["snapshot"].last_pipeline_at else None,
                    "lifecycle": row["lifecycle"],
                }
                for row in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch audit summary: {str(e)}")


# ── Config Scan API ────────────────────────────────────────────────


class ConfigScanRunRequest(BaseModel):
    product_ids: List[int]


class ConfigScanRepositoryRequest(BaseModel):
    product_id: int
    repository_id: int


@router.post("/config-scan/run")
async def api_config_scan_run(
    request: Request,
    body: ConfigScanRunRequest,
    token: str = Depends(verify_bearer_token),
):
    """Trigger config scan for specified products."""
    try:
        poller = request.app.state.config_scan_poller
        run_ids = await poller.trigger_manual_scan(body.product_ids)
        return {"status": "accepted", "run_ids": run_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to trigger config scan: {str(e)}")


@router.post("/config-scan/repository")
async def api_config_scan_repository(
    request: Request,
    body: ConfigScanRepositoryRequest,
    db: Session = Depends(get_db),
    token: str = Depends(verify_bearer_token),
):
    """Trigger config scan for a single repository (same as UI on /products/{id})."""
    repo = (
        db.query(RepositoryModel)
        .filter(RepositoryModel.id == body.repository_id)
        .first()
    )
    if not repo or repo.product_id != body.product_id:
        raise HTTPException(status_code=404, detail="Repository not found for product")
    try:
        poller = request.app.state.config_scan_poller
        run_id = await poller.trigger_repo_scan(body.product_id, body.repository_id)
        return {"status": "accepted", "run_id": run_id}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to trigger repository scan: {str(e)}")


@router.get("/config-scan/runs/{run_id}")
async def api_config_scan_run_status(
    run_id: int,
    db: Session = Depends(get_db),
):
    """Return status and summary of a config scan run."""
    try:
        service = ConfigScanService(db)
        run = service.get_run_by_id(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Config scan run not found")
        sc_disp, tot_disp = project_counts_for_run(run)
        return {
            "id": run.id,
            "product_id": run.product_id,
            "status": run.status,
            "trigger": run.trigger,
            "total_projects": tot_disp,
            "scanned_projects": sc_disp,
            "findings_count": run.findings_count,
            "summary_json": run.summary_json,
            "error_text": run.error_text,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch config scan run: {str(e)}")
