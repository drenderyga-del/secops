"""Web application entrypoint (UI + API).

Background services live in their own processes now:

- ``python -m app.workers.pipeline`` — GitLab pipeline poller, long-running
  job alerts, retention cleanup;
- ``python -m app.iac_worker`` — IaC scanning worker.

This process keeps only the ConfigScanPoller (the UI triggers manual scans
through it on app.state, and the monthly scheduler is cheap) and serves
HTTP. No DB work happens at import time — schema setup and migrations run in
the startup event, so ``import app.main`` works without a database (tests).
"""
import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config_scan_poller import ConfigScanPoller
from .config_scan_service import ConfigScanService
from .iac_service import IacService
from .routers import api_router, web_router

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

config_scan_poller = ConfigScanPoller()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown (replaces deprecated @app.on_event handlers)."""
    logger.info(
        "web_app_start log_level=%s config_scan_enabled=%s ui_auth_configured=%s",
        os.getenv("LOG_LEVEL", "INFO").upper(),
        os.getenv("CONFIG_SCAN_ENABLED", "true"),
        _ui_auth_configured(),
    )
    if not _ui_auth_configured():
        logger.warning(
            "web_auth_disabled reason=missing_ui_auth_credentials"
        )
    _setup_database()
    _recover_interrupted_runs()
    await config_scan_poller.start()
    try:
        yield
    finally:
        await config_scan_poller.stop()
        logger.info("web_app_stop")


app = FastAPI(
    title="ScanOps",
    description="Security execution and evidence control plane",
    version="1.3.8",
    lifespan=lifespan,
)

@app.exception_handler(RequestValidationError)
async def integration_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith(("/api/v1/agent/", "/api/v1/agent-operations/", "/api/v1/asoc/")):
        # Pydantic's default errors contain the rejected input (possibly a secret).
        errors = [{key: error[key] for key in ("loc", "type", "msg")} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})
    return await request_validation_exception_handler(request, exc)


# ── Authentication ───────────────────────────────────────────────────
# One shared Basic-auth account for the team protects everything: UI pages,
# static files and previously unauthenticated GET API endpoints. CI keeps
# using `Authorization: Bearer $PIPE_BEARER_TOKEN`. /api/health stays open
# for the docker healthcheck.

_AUTH_EXEMPT_PATHS = {"/api/health"}
_OPERATOR_ONLY_PATHS = {
    "/api/ui/asoc-reporting/sync",
}


def _ui_auth_configured() -> bool:
    return bool(os.getenv("UI_AUTH_USERNAME") and os.getenv("UI_AUTH_PASSWORD"))


def _basic_auth_ok(header: str) -> bool:
    try:
        encoded = header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    return secrets.compare_digest(
        username.encode(), os.getenv("UI_AUTH_USERNAME", "").encode()
    ) and secrets.compare_digest(password.encode(), os.getenv("UI_AUTH_PASSWORD", "").encode())


def _bearer_auth_ok(header: str) -> bool:
    expected = os.getenv("PIPE_BEARER_TOKEN", "")
    if not expected:
        return False
    token = header.split(" ", 1)[1] if " " in header else ""
    return secrets.compare_digest(token.encode(), expected.encode())


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _csrf_ok(request: Request) -> bool:
    """Same-origin check for browser (Basic-auth) mutating requests.

    A state-changing request from a real browser always carries Origin (or at
    least Referer); it must match the served host. Bearer/CI callers are exempt
    (checked by the caller) since they are not cookie/Basic browser sessions.
    """
    if request.method in _SAFE_METHODS:
        return True
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return False
    return urlparse(source).netloc == request.headers.get("host", "")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    from .agent_triage import agent_credential_conflict
    if path not in _AUTH_EXEMPT_PATHS and agent_credential_conflict():
        return JSONResponse(status_code=503, content={"detail": "Authentication credentials must be distinct; rotate the agent credential"})
    # Bound new integration payloads before JSON parsing, including chunked uploads.
    if path.startswith(("/api/v1/agent/", "/api/v1/agent-operations/", "/api/v1/asoc/")):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 6 * 1024 * 1024:
                return JSONResponse(status_code=413, content={"detail": "Request exceeds 6 MiB"})
            chunks.append(chunk)
        request._body = b"".join(chunks)
    # The dedicated agent credential is scoped to this namespace only.
    # Route dependencies still enforce it when UI authentication is disabled.
    if path.startswith("/api/v1/agent/"):
        from .agent_triage import agent_token_ok
        if not agent_token_ok(request.headers.get("authorization", "")):
            return JSONResponse(status_code=401, content={"detail": "Dedicated agent authentication required"})
        return await call_next(request)
    if path in _OPERATOR_ONLY_PATHS:
        # ASOC refresh replaces the canonical product projection. Unlike
        # normal CI APIs, this state-changing operation is only for an
        # authenticated operator and must fail closed when UI auth is unset.
        if not _ui_auth_configured():
            return JSONResponse(
                status_code=503,
                content={"detail": "Operator authentication is not configured"},
            )
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic ") or not _basic_auth_ok(header):
            return JSONResponse(
                status_code=401,
                content={"detail": "Operator authentication required"},
                headers={"WWW-Authenticate": 'Basic realm="ScanOps operator"'},
            )
        if not _csrf_ok(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF check failed: Origin/Referer mismatch"},
            )
        return await call_next(request)
    if not _ui_auth_configured():
        return await call_next(request)
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    # Bearer = CI/API token, not a browser session → no CSRF surface.
    if header.startswith("Bearer ") and _bearer_auth_ok(header):
        return await call_next(request)
    if header.startswith("Basic ") and _basic_auth_ok(header):
        if not _csrf_ok(request):
            return JSONResponse(status_code=403, content={"detail": "CSRF check failed: Origin/Referer mismatch"})
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required"},
        headers={"WWW-Authenticate": 'Basic realm="ScanOps"'},
    )


# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Include routers
from .routers.agent_api import router as agent_router
from .routers.asoc_api import router as asoc_router
app.include_router(asoc_router)
app.include_router(agent_router)
app.include_router(api_router)
app.include_router(web_router)

# Expose config_scan_poller on app state so routers can trigger manual scans
app.state.config_scan_poller = config_scan_poller


def _audit_restart_diagnostic(previous_status: str) -> tuple[str, str]:
    if previous_status == "queued":
        return (
            "Queued audit scan did not start before service restart",
            "Not started before app restart",
        )
    return (
        "Audit scan interrupted by service restart",
        "Interrupted by app restart",
    )


def _setup_database() -> None:
    """Create tables, apply tracked migrations, run one-off backfills."""
    from .asoc_description_safety import sanitize_existing_asoc_descriptions
    from .maintenance import backfill_duration_columns
    from .migrations_runner import PYTHON_MIGRATIONS, run_migrations, schema_bootstrap_lock
    from .models import Base, engine

    with schema_bootstrap_lock(engine):
        Base.metadata.create_all(bind=engine)
        PYTHON_MIGRATIONS.setdefault(
            "20260925_sanitize_asoc_descriptions",
            sanitize_existing_asoc_descriptions,
        )
        PYTHON_MIGRATIONS.setdefault("20260610_backfill_durations", backfill_duration_columns)
        run_migrations(engine)


def _recover_interrupted_runs() -> None:
    from datetime import datetime, timezone

    from .models import AuditScanRun, SessionLocal

    db = SessionLocal()
    try:
        stuck = (
            db.query(AuditScanRun)
            .filter(AuditScanRun.status.in_(["queued", "running"]))
            .all()
        )
        if stuck:
            now = datetime.now(timezone.utc)
            for run in stuck:
                message, error_message = _audit_restart_diagnostic(run.status)
                run.status = "failed"
                run.finished_at = now
                run.message = message
                if not run.error_message:
                    run.error_message = error_message
                db.add(run)
            db.commit()
            logger.warning("startup_recovered_runs type=audit count=%s", len(stuck))

        count = ConfigScanService(db).recover_interrupted_runs()
        if count:
            logger.warning("startup_recovered_runs type=config_scan count=%s", count)

        # IaC runs are NOT recovered here: they execute in the separate
        # iac_worker container, which recovers its own interrupted runs on ITS
        # startup. Failing them from a web deploy killed queued/running scans.
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
