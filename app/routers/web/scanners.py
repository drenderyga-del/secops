"""Unified scanner fleet view for the current ScanOps adapters."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...integration_settings import connector_snapshot
from ...models import ActivePipeline, ConfigScanRun, IacScanRun, JobReport, PtaiScan
from ...models.job_report import JobStatus
from ...models.pipeline import PipelineStatus
from .common import get_db, templates

router = APIRouter(tags=["web"])


def _run_state(status: str | None) -> str:
    normalized = (status or "").lower()
    if normalized in {"queued", "running", "polling", "in_progress", "pending"}:
        return "running"
    if normalized in {"failed", "error", "trigger_failed", "partial_success"}:
        return "error"
    if normalized in {"succeeded", "success", "completed", "done"}:
        return "healthy"
    return "idle"


def _status_label(status: str | None, *, empty: str = "No run recorded") -> str:
    normalized = (status or "").strip()
    return normalized.replace("_", " ").title() if normalized else empty


def _connection_contract(snapshot: dict, source_name: str) -> dict:
    """Describe connection readiness without exposing configuration values."""
    if not snapshot.get("enabled", True):
        return {
            "state": "disabled",
            "label": "Disabled",
            "reason": f"Source: {source_name} is disabled in Integrations.",
        }
    if snapshot.get("error"):
        return {
            "state": "degraded",
            "label": "Unavailable",
            "reason": f"Source: {source_name} configuration could not be loaded.",
        }
    if not snapshot.get("configured"):
        return {
            "state": "degraded",
            "label": "Setup required",
            "reason": f"Source: {source_name} is not configured in Integrations.",
        }
    if snapshot.get("test_status") == "failed":
        message = (
            snapshot.get("test_message") or "the latest connection test failed"
        ).strip()
        return {
            "state": "degraded",
            "label": "Test failed",
            "reason": f"Source: {source_name} — {message[:240]}",
        }
    if snapshot.get("test_status") == "healthy":
        return {
            "state": "healthy",
            "label": "Connected",
            "reason": f"Source: {source_name} passed its latest connection test.",
        }
    return {
        "state": "unknown",
        "label": "Configured · untested",
        "reason": (
            f"Source: {source_name} is configured; no connection test is recorded."
        ),
    }


def _combined_connection_contract(parts: list[tuple[str, dict]]) -> dict:
    problems = [
        (name, contract)
        for name, contract in parts
        if contract["state"] not in {"healthy", "unknown"}
    ]
    if problems:
        names = " and ".join(name for name, _contract in problems)
        reasons = " ".join(contract["reason"] for _name, contract in problems)
        verb = "needs" if len(problems) == 1 else "need"
        return {
            "state": "degraded",
            "label": f"{names} {verb} attention",
            "reason": reasons,
        }
    untested = [
        (name, contract)
        for name, contract in parts
        if contract["state"] == "unknown"
    ]
    if untested:
        names = " + ".join(name for name, _contract in untested)
        return {
            "state": "unknown",
            "label": f"{names} configured · untested",
            "reason": " ".join(contract["reason"] for _name, contract in parts),
        }
    names = " + ".join(name for name, _contract in parts)
    return {
        "state": "healthy",
        "label": f"{names} ready",
        "reason": " ".join(contract["reason"] for _name, contract in parts),
    }


def _ptai_connection_contract(asoc: dict, ptai: dict) -> dict:
    """ASOC is required; direct PT AI telemetry remains optional."""
    if asoc["state"] == "unknown":
        return {
            "state": "unknown",
            "label": "ASOC configured · untested",
            "reason": asoc["reason"],
        }
    if asoc["state"] != "healthy":
        return {
            "state": "degraded",
            "label": "ASOC needs attention",
            "reason": asoc["reason"],
        }
    if ptai["state"] == "healthy":
        return _combined_connection_contract([("ASOC", asoc), ("PT AI", ptai)])
    if ptai["state"] == "unknown":
        return {
            "state": "unknown",
            "label": "PT AI configured · untested",
            "reason": f"{asoc['reason']} {ptai['reason']}",
        }
    if ptai["label"] in {"Setup required", "Disabled"}:
        return {
            "state": "healthy",
            "label": "ASOC ready",
            "reason": (
                f"{asoc['reason']} Direct PT AI telemetry is optional and is "
                "not active."
            ),
        }
    return {
        "state": "degraded",
        "label": "PT AI telemetry degraded",
        "reason": f"{asoc['reason']} {ptai['reason']}",
    }


def _latest_contract(
    status: str | None,
    occurred_at: datetime | None,
    *,
    empty_reason: str,
    summary: str | None = None,
) -> dict:
    if not status:
        return {
            "state": "idle",
            "label": "No run recorded",
            "occurred_at": None,
            "summary": empty_reason,
        }
    return {
        "state": _run_state(status),
        "label": _status_label(status),
        "occurred_at": occurred_at,
        "summary": summary or "Latest execution state is available.",
    }


def _ready_connection_count(scanners: list[dict]) -> int:
    """Count only adapters whose required connections passed a test."""
    return sum(
        1
        for scanner in scanners
        if scanner["connection"]["state"] == "healthy"
    )


@router.get("/classic/scanners", response_class=RedirectResponse, include_in_schema=False)
async def scanners_compatibility(request: Request):
    query = urlencode(
        [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key != "status"
        ],
        doseq=True,
    )
    return RedirectResponse(
        f"/scanners/operator?{query}" if query else "/scanners/operator",
        status_code=308,
    )


@router.get("/scanners/operator", response_class=HTMLResponse)
async def scanner_fleet_operator(request: Request, db: Session = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(days=7)
    observed_steps = (
        db.query(func.count(JobReport.id))
        .filter(JobReport.finished_at >= since)
        .scalar()
        or 0
    )
    failed_steps = (
        db.query(func.count(JobReport.id))
        .filter(
            JobReport.finished_at >= since,
            JobReport.job_status == JobStatus.FAILED,
        )
        .scalar()
        or 0
    )
    active_pipelines = (
        db.query(func.count(ActivePipeline.id))
        .filter(
            ActivePipeline.status.in_(
                [PipelineStatus.REGISTERED, PipelineStatus.POLLING]
            )
        )
        .scalar()
        or 0
    )

    latest_job = db.query(JobReport).order_by(JobReport.finished_at.desc()).first()
    config_run = (
        db.query(ConfigScanRun).order_by(ConfigScanRun.created_at.desc()).first()
    )
    iac_run = db.query(IacScanRun).order_by(IacScanRun.created_at.desc()).first()
    ptai_latest = db.query(PtaiScan).order_by(PtaiScan.created_at.desc()).first()

    config_runs_7d = (
        db.query(func.count(ConfigScanRun.id))
        .filter(ConfigScanRun.created_at >= since)
        .scalar()
        or 0
    )
    config_projects_7d = (
        db.query(func.coalesce(func.sum(ConfigScanRun.scanned_projects), 0))
        .filter(ConfigScanRun.created_at >= since)
        .scalar()
        or 0
    )
    iac_runs_7d = (
        db.query(func.count(IacScanRun.id))
        .filter(IacScanRun.created_at >= since)
        .scalar()
        or 0
    )
    iac_repositories_7d = (
        db.query(func.coalesce(func.sum(IacScanRun.scanned_repositories), 0))
        .filter(IacScanRun.created_at >= since)
        .scalar()
        or 0
    )
    ptai_runs_7d = (
        db.query(func.count(PtaiScan.id))
        .filter(PtaiScan.created_at >= since)
        .scalar()
        or 0
    )
    ptai_problems_7d = (
        db.query(func.count(PtaiScan.id))
        .filter(
            PtaiScan.created_at >= since,
            PtaiScan.asoc_status.in_(
                ["ERROR", "PARTIAL_SUCCESS", "TRIGGER_FAILED"]
            ),
        )
        .scalar()
        or 0
    )

    gitlab_connection = _connection_contract(
        connector_snapshot(db, "gitlab"),
        "GitLab",
    )
    asoc_connection = _connection_contract(
        connector_snapshot(db, "asoc"),
        "ASOC",
    )
    ptai_connection = _connection_contract(
        connector_snapshot(db, "ptai"),
        "PT AI",
    )

    latest_job_status = latest_job.job_status.label if latest_job else None
    config_status = config_run.status if config_run else None
    iac_status = iac_run.status if iac_run else None
    ptai_status = ptai_latest.asoc_status if ptai_latest else None

    scanners = [
        {
            "key": "gitlab",
            "name": "GitLab security jobs",
            "role": "Pipeline-native execution adapter",
            "description": (
                "SAST, SCA, Secrets, container and mobile jobs observed "
                "directly from CI."
            ),
            "connection": gitlab_connection,
            "integration_href": "/integrations?integration=gitlab",
            "integration_action": "Manage GitLab connection",
            "latest": _latest_contract(
                latest_job_status,
                latest_job.finished_at if latest_job else None,
                empty_reason=(
                    "Source: no GitLab security job reports have been received."
                ),
                summary=(
                    f"{latest_job.job_name} reported {latest_job_status}."
                    if latest_job
                    else None
                ),
            ),
            "workload": [
                {"label": "Observed · 7d", "value": observed_steps},
                {"label": "Failed · 7d", "value": failed_steps},
            ],
            "href": "/logs",
            "action": "Open executions",
        },
        {
            "key": "config",
            "name": "Config Guard",
            "role": "Configuration execution adapter",
            "description": (
                "Protected branches, approvals, variables, includes and "
                "pipeline configuration drift."
            ),
            "connection": gitlab_connection,
            "integration_href": "/integrations?integration=gitlab",
            "integration_action": "Manage GitLab connection",
            "latest": _latest_contract(
                config_status,
                (
                    config_run.finished_at
                    or config_run.started_at
                    or config_run.created_at
                    if config_run
                    else None
                ),
                empty_reason="Source: Config Guard has no recorded scan run.",
                summary=(
                    f"{config_run.scanned_projects} projects scanned; "
                    f"{config_run.findings_count} findings."
                    if config_run
                    else None
                ),
            ),
            "workload": [
                {"label": "Runs · 7d", "value": config_runs_7d},
                {"label": "Projects · 7d", "value": config_projects_7d},
            ],
            "href": "/config-scan",
            "action": "Open Config Guard",
        },
        {
            "key": "iac",
            "name": "Checkov · IaC",
            "role": "Infrastructure-as-Code execution adapter",
            "description": (
                "Repository discovery, Checkov execution, SARIF normalization "
                "and ASOC delivery."
            ),
            "connection": _combined_connection_contract(
                [("GitLab", gitlab_connection), ("ASOC", asoc_connection)]
            ),
            "integration_href": "/integrations",
            "integration_action": "Manage GitLab and ASOC connections",
            "latest": _latest_contract(
                iac_status,
                (
                    iac_run.finished_at or iac_run.started_at or iac_run.created_at
                    if iac_run
                    else None
                ),
                empty_reason="Source: the IaC runtime has no recorded scan run.",
                summary=(
                    f"{iac_run.scanned_repositories} repositories scanned; "
                    f"{iac_run.findings_count} findings."
                    if iac_run
                    else None
                ),
            ),
            "workload": [
                {"label": "Runs · 7d", "value": iac_runs_7d},
                {"label": "Repositories · 7d", "value": iac_repositories_7d},
            ],
            "href": "/iac",
            "action": "Open IaC scanner",
        },
        {
            "key": "ptai",
            "name": "PT AI",
            "role": "Application security execution adapter",
            "description": (
                "GitLab-triggered scans tracked through ASOC and enriched with "
                "direct engine progress."
            ),
            "connection": _ptai_connection_contract(
                asoc_connection,
                ptai_connection,
            ),
            "integration_href": "/integrations",
            "integration_action": "Manage ASOC and PT AI connections",
            "latest": _latest_contract(
                ptai_status,
                (
                    ptai_latest.finished_at
                    or ptai_latest.last_polled_at
                    or ptai_latest.created_at
                    if ptai_latest
                    else None
                ),
                empty_reason="Source: ASOC has no tracked PT AI scan.",
                summary=(
                    f"ASOC reported {_status_label(ptai_status).lower()}."
                    if ptai_latest
                    else None
                ),
            ),
            "workload": [
                {"label": "Started · 7d", "value": ptai_runs_7d},
                {"label": "Problems · 7d", "value": ptai_problems_7d},
            ],
            "href": "/ptai-scans",
            "action": "Open PT AI",
        },
    ]

    return templates.TemplateResponse(
        "scanners.html",
        {
            "request": request,
            "scanners": scanners,
            "observed_steps": observed_steps,
            "failed_steps": failed_steps,
            "active_pipelines": active_pipelines,
            "ready_connections": _ready_connection_count(scanners),
            "running_executions": sum(
                1
                for scanner in scanners
                if scanner["latest"]["state"] == "running"
            ),
            "last_activity": max(
                [
                    value
                    for value in [
                        latest_job.finished_at if latest_job else None,
                        config_run.created_at if config_run else None,
                        iac_run.created_at if iac_run else None,
                        ptai_latest.created_at if ptai_latest else None,
                    ]
                    if value
                ],
                default=None,
            ),
        },
    )
