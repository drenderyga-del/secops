"""Operator-facing integration registry and credential management."""
from __future__ import annotations

import os
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ...integration_settings import (
    CONNECTOR_SPECS,
    CredentialStoreUnavailable,
    IntegrationConfigError,
    connector_snapshot,
    create_gitlab_instance,
    credential_store_ready,
    delete_gitlab_instance,
    disable_connector,
    load_runtime_connector,
    record_test_result,
    resolve_gitlab_instance,
    save_connector,
    save_gitlab_instance,
    test_connector_connection,
    test_gitlab_instance_connection,
)
from ...connector_registry import refresh_registry
from .common import get_db, templates

router = APIRouter(tags=["web"])


def _ui_admin() -> str:
    username = (os.getenv("UI_AUTH_USERNAME") or "").strip()
    password = (os.getenv("UI_AUTH_PASSWORD") or "").strip()
    if not username or not password:
        raise HTTPException(
            status_code=403,
            detail="Integration settings are read-only until UI authentication is enabled",
        )
    return username


def _redirect(
    key: str,
    message: str,
    tone: str = "success",
    *,
    keep_open: bool = True,
) -> RedirectResponse:
    params = {"notice": message[:240], "tone": tone}
    if keep_open:
        params["focus"] = key
    query = urlencode(params)
    fragment = f"#integration-{key}-settings" if keep_open else ""
    return RedirectResponse(url=f"/integrations?{query}{fragment}", status_code=303)


def _gitlab_instance_redirect(
    instance_id: str,
    message: str,
    tone: str = "success",
) -> RedirectResponse:
    query = urlencode(
        {
            "notice": message[:240],
            "tone": tone,
            "focus": "gitlab",
            "instance": instance_id,
        }
    )
    return RedirectResponse(
        url=f"/integrations?{query}#integration-gitlab-instance-{instance_id}",
        status_code=303,
    )


def _reject_bad_instance_reference(exc: IntegrationConfigError) -> None:
    message = str(exc)
    if "identifier is invalid" in message:
        raise HTTPException(status_code=400, detail=message)
    if "identifier does not match" in message:
        raise HTTPException(status_code=404, detail=message)


def _record_gitlab_instance_result(
    db: Session,
    *,
    instance_id: str,
    actor: str,
    success: bool,
    message: str,
) -> None:
    try:
        record_test_result(
            db,
            "gitlab",
            actor=actor,
            success=success,
            message=message,
            instance_id=instance_id,
        )
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "GitLab instance configuration changed during the connection test; "
                "reload and retry"
            ),
        ) from exc


@router.get("/classic/integrations", response_class=RedirectResponse, include_in_schema=False)
async def integrations_compatibility(request: Request):
    """Retire the duplicate credential UI while preserving bookmarked focus."""
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "focus"
    ]
    focus = request.query_params.get("focus", "")
    if focus:
        params.append(("integration", focus))
    query = urlencode(params, doseq=True)
    return RedirectResponse(
        url=f"/integrations?{query}" if query else "/integrations",
        status_code=308,
    )


@router.get(
    "/integrations/operator",
    response_class=HTMLResponse,
    name="integrations_operator",
)
async def integrations(request: Request, db: Session = Depends(get_db)):
    connectors = [connector_snapshot(db, key) for key in CONNECTOR_SPECS]
    registry_by_key = {item["key"]: item for item in refresh_registry(db)["connectors"]}
    for connector in connectors:
        connector["registry"] = registry_by_key.get(connector["key"])
    auth_ready = bool(os.getenv("UI_AUTH_USERNAME") and os.getenv("UI_AUTH_PASSWORD"))
    store_ready = credential_store_ready()
    focus = request.query_params.get("focus", "")
    instance_focus = request.query_params.get("instance", "")
    if focus and focus not in CONNECTOR_SPECS:
        raise HTTPException(status_code=400, detail="Unknown integration focus")
    if instance_focus:
        if focus != "gitlab":
            raise HTTPException(
                status_code=400,
                detail="GitLab instance focus requires focus=gitlab",
            )
        if not instance_focus.startswith("gl-") or len(instance_focus) != 19:
            raise HTTPException(status_code=400, detail="GitLab instance identifier is invalid")
        gitlab_snapshot = next(
            connector for connector in connectors if connector["key"] == "gitlab"
        )
        if instance_focus not in {
            instance["id"] for instance in gitlab_snapshot.get("instances", [])
        }:
            raise HTTPException(
                status_code=404,
                detail="GitLab instance identifier does not match stored configuration",
            )
    return templates.TemplateResponse(
        "integrations.html",
        {
            "request": request,
            "connectors": connectors,
            "configured_count": sum(1 for connector in connectors if connector["configured"]),
            "can_manage_integrations": auth_ready and store_ready,
            "integration_auth_ready": auth_ready,
            "credential_store_ready": store_ready,
            "focus": focus,
            "instance_focus": instance_focus,
            "notice": request.query_params.get("notice", ""),
            "notice_tone": request.query_params.get("tone", "success"),
        },
    )


@router.post("/integrations/{key}/save")
async def integration_save(
    key: str,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = _ui_admin()
    if key not in CONNECTOR_SPECS:
        raise HTTPException(status_code=404, detail="Unknown connector")
    form = await request.form()
    submitted = {str(name): str(value) for name, value in form.items()}
    try:
        save_connector(db, key, submitted, actor=actor)
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        return _redirect(key, str(exc), "error")
    return _redirect(
        key,
        f"{CONNECTOR_SPECS[key]['name']} configuration saved",
        keep_open=False,
    )


@router.post("/integrations/gitlab/instances/{instance_id}/save")
async def gitlab_instance_save(
    instance_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = _ui_admin()
    form = await request.form()
    submitted = {str(name): str(value) for name, value in form.items()}
    try:
        save_gitlab_instance(db, instance_id, submitted, actor=actor)
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        _reject_bad_instance_reference(exc)
        return _gitlab_instance_redirect(instance_id, str(exc), "error")
    return _gitlab_instance_redirect(instance_id, "GitLab instance configuration saved")


@router.post("/integrations/gitlab/instances")
async def gitlab_instance_create(
    request: Request,
    db: Session = Depends(get_db),
):
    actor = _ui_admin()
    form = await request.form()
    submitted = {str(name): str(value) for name, value in form.items()}
    try:
        _row, instance_id = create_gitlab_instance(
            db, submitted, actor=actor
        )
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        return _redirect("gitlab", str(exc), "error")
    return _gitlab_instance_redirect(instance_id, "GitLab instance created")


@router.post("/integrations/gitlab/instances/{instance_id}/delete")
async def gitlab_instance_delete(
    instance_id: str,
    db: Session = Depends(get_db),
):
    actor = _ui_admin()
    try:
        delete_gitlab_instance(db, instance_id, actor=actor)
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        _reject_bad_instance_reference(exc)
        return _gitlab_instance_redirect(instance_id, str(exc), "error")
    return _redirect("gitlab", "GitLab instance removed", "warning", keep_open=False)


@router.post("/integrations/{key}/disable")
async def integration_disable(
    key: str,
    db: Session = Depends(get_db),
):
    actor = _ui_admin()
    try:
        disable_connector(db, key, actor=actor)
    except IntegrationConfigError as exc:
        db.rollback()
        return _redirect(key, str(exc), "error")
    return _redirect(
        key,
        f"{CONNECTOR_SPECS[key]['name']} disabled",
        "warning",
        keep_open=False,
    )


@router.post("/integrations/{key}/test")
async def integration_test(
    key: str,
    db: Session = Depends(get_db),
):
    actor = _ui_admin()
    if key not in CONNECTOR_SPECS:
        raise HTTPException(status_code=404, detail="Unknown connector")
    runtime = load_runtime_connector(key, db)
    try:
        message = await run_in_threadpool(test_connector_connection, runtime)
    except requests.Timeout:
        db.rollback()
        message = "Connection test timed out; check routing, proxy and endpoint availability"
        record_test_result(db, key, actor=actor, success=False, message=message)
        return _redirect(key, message, "error")
    except requests.exceptions.SSLError:
        db.rollback()
        message = "TLS verification failed; install the issuing CA in the application trust store"
        record_test_result(db, key, actor=actor, success=False, message=message)
        return _redirect(key, message, "error")
    except requests.RequestException:
        db.rollback()
        message = "Connection test failed: endpoint is unreachable; check DNS and routing"
        record_test_result(db, key, actor=actor, success=False, message=message)
        return _redirect(key, message, "error")
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        message = str(exc) or "Connection test failed"
        record_test_result(db, key, actor=actor, success=False, message=message)
        return _redirect(key, message, "error")
    record_test_result(db, key, actor=actor, success=True, message=message)
    return _redirect(key, message)


@router.post("/integrations/gitlab/instances/{instance_id}/test")
async def gitlab_instance_test(
    instance_id: str,
    db: Session = Depends(get_db),
):
    actor = _ui_admin()
    runtime = load_runtime_connector("gitlab", db)
    try:
        resolve_gitlab_instance(runtime, instance_id)
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        _reject_bad_instance_reference(exc)
        return _gitlab_instance_redirect(instance_id, str(exc), "error")
    try:
        message = await run_in_threadpool(
            test_gitlab_instance_connection, runtime, instance_id
        )
    except requests.Timeout:
        db.rollback()
        message = "Connection test timed out; check routing, proxy and endpoint availability"
        _record_gitlab_instance_result(
            db,
            instance_id=instance_id,
            actor=actor,
            success=False,
            message=message,
        )
        return _gitlab_instance_redirect(instance_id, message, "error")
    except requests.exceptions.SSLError:
        db.rollback()
        message = "TLS verification failed; install the issuing CA in the application trust store"
        _record_gitlab_instance_result(
            db,
            instance_id=instance_id,
            actor=actor,
            success=False,
            message=message,
        )
        return _gitlab_instance_redirect(instance_id, message, "error")
    except requests.RequestException:
        db.rollback()
        message = "Connection test failed: endpoint is unreachable; check DNS and routing"
        _record_gitlab_instance_result(
            db,
            instance_id=instance_id,
            actor=actor,
            success=False,
            message=message,
        )
        return _gitlab_instance_redirect(instance_id, message, "error")
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        db.rollback()
        message = str(exc) or "Connection test failed"
        _record_gitlab_instance_result(
            db,
            instance_id=instance_id,
            actor=actor,
            success=False,
            message=message,
        )
        return _gitlab_instance_redirect(instance_id, message, "error")
    _record_gitlab_instance_result(
        db,
        instance_id=instance_id,
        actor=actor,
        success=True,
        message=message,
    )
    return _gitlab_instance_redirect(instance_id, message)
