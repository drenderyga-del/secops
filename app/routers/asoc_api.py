"""Multiple ASOC destinations behind explicit provider capabilities."""
from __future__ import annotations
import hashlib
import json
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..agent_triage import checked_text, require_operator
from ..asoc_adapters import AsocAdapter, AsocAdapterError, PROVIDERS, validate_base_url, validate_settings, encrypt_destination_token
from ..integration_settings import CredentialStoreUnavailable
from ..models import AsocDestination, AsocReportDelivery
from .api import get_db, verify_bearer_token

router = APIRouter(prefix="/api/v1/asoc", tags=["ASOC adapters"])


class Destination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    provider: Literal["vampy", "defectdojo", "dependencytrack"]
    api_base_url: str = Field(min_length=1, max_length=2048)
    token: str = Field(min_length=8, max_length=4096)
    settings: dict[str, str] = Field(default_factory=dict, max_length=5)
    enabled: bool = True


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,80}$")
    report_format: Literal["sarif", "cyclonedx"]
    report: dict


def _safe(row):
    return {"id": row.id, "config_version": row.config_version, "name": row.name, "provider": row.provider, "api_base_url": row.api_base_url,
            "settings": json.loads(row.settings_json), "enabled": row.enabled,
            "credential_configured": bool(row.secrets_encrypted), "capabilities": PROVIDERS[row.provider]}


def _destination(db, destination_id, *, lock=False):
    query = db.query(AsocDestination).filter_by(id=destination_id)
    row = (query.with_for_update().populate_existing() if lock else query).first()
    if not row:
        raise HTTPException(status_code=404, detail="ASOC destination not found")
    return row


@router.get("/providers")
def providers():
    return {"providers": [{"key": key, **value} for key, value in PROVIDERS.items()]}


@router.get("/destinations")
def destinations(db: Session = Depends(get_db), actor: str = Depends(require_operator)):
    return {"destinations": [_safe(row) for row in db.query(AsocDestination).order_by(AsocDestination.id).all()]}


def _save(db, body, actor, row=None):
    try:
        url = validate_base_url(body.api_base_url)
        settings = validate_settings(body.provider, body.settings)
        checked_text(body.name)
        checked_text(url)
        for value in settings.values():
            checked_text(value)
        # Reject header injection before encrypting the token.
        if any(c.isspace() or ord(c) < 32 for c in body.token):
            raise AsocAdapterError("Credential must not contain whitespace or control characters")
        if any(body.token in value for value in [body.name, url, *settings.values()]):
            raise AsocAdapterError("Credential must appear only in the token field")
        encrypted = encrypt_destination_token(body.token)
    except AsocAdapterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except CredentialStoreUnavailable:
        raise HTTPException(status_code=503, detail="Integration credential storage is unavailable") from None
    row = row or AsocDestination()
    row.config_version = str(uuid4())
    row.name, row.provider, row.api_base_url = body.name, body.provider, url
    row.settings_json, row.secrets_encrypted = json.dumps(settings), encrypted
    row.enabled, row.updated_by = body.enabled, actor
    db.add(row)
    db.commit()
    return _safe(row)


@router.post("/destinations", status_code=201)
def create_destination(body: Destination, db: Session = Depends(get_db), actor: str = Depends(require_operator)):
    return _save(db, body, actor)


@router.put("/destinations/{destination_id}")
def update_destination(destination_id: int, body: Destination, db: Session = Depends(get_db), actor: str = Depends(require_operator)):
    return _save(db, body, actor, _destination(db, destination_id, lock=True))


@router.post("/destinations/{destination_id}/check")
def check_destination(destination_id: int, db: Session = Depends(get_db), actor: str = Depends(require_operator)):
    try:
        receipt = AsocAdapter(_destination(db, destination_id)).check()
    except (AsocAdapterError, CredentialStoreUnavailable) as exc:
        message = str(exc) if isinstance(exc, AsocAdapterError) else "Credential storage unavailable"
        raise HTTPException(status_code=502 if isinstance(exc, AsocAdapterError) else 503, detail=message) from None
    return {"state": receipt.state}


@router.post("/destinations/{destination_id}/reports", status_code=202)
def deliver_report(destination_id: int, body: Report, db: Session = Depends(get_db), token: str = Depends(verify_bearer_token)):
    row = _destination(db, destination_id, lock=True)
    digest = hashlib.sha256(json.dumps(body.report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = db.query(AsocReportDelivery).filter_by(request_id=body.request_id).first()
    if previous:
        if (previous.destination_id != row.id or previous.digest_sha256 != digest
            or previous.report_format != body.report_format):
            raise HTTPException(status_code=409, detail="Request ID already used for another delivery")
        if previous.config_version != row.config_version:
            raise HTTPException(status_code=409, detail={"delivery_id": previous.id,
                "message": "Destination configuration changed; inspect the original target before requesting another send"})
        if previous.state != "accepted":
            raise HTTPException(status_code=409, detail={"delivery_id": previous.id,
                "state": previous.state, "message": "Inspect the destination before requesting another send"})
        return {"delivery_id": previous.id, "config_version": previous.config_version, "state": previous.state, "reference": previous.remote_reference,
                "digest_sha256": previous.digest_sha256, "processing_verified": False}
    try:
        # Capture one coherent configuration before the journal commit expires
        # ORM state; a concurrent edit must not change the provider mid-send.
        adapter = AsocAdapter(row)
    except (AsocAdapterError, CredentialStoreUnavailable) as exc:
        message = str(exc) if isinstance(exc, AsocAdapterError) else "Credential storage unavailable"
        raise HTTPException(status_code=422 if isinstance(exc, AsocAdapterError) else 503, detail=message) from None
    delivery = AsocReportDelivery(request_id=body.request_id, destination_id=row.id, provider=adapter.provider,
                                 config_version=row.config_version,
                                 target_json=json.dumps({"provider": adapter.provider, "api_base_url": adapter.url,
                                                         "settings": adapter.settings}, sort_keys=True),
                                 report_format=body.report_format, digest_sha256=digest, state="pending")
    # Persist the attempt before sending; an interrupted send remains unknown.
    db.add(delivery)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Concurrent delivery; retry the same request ID") from None
    try:
        receipt = adapter.send(body.report_format, body.report)
    except (AsocAdapterError, CredentialStoreUnavailable) as exc:
        delivery.state = "unknown" if isinstance(exc, AsocAdapterError) and exc.uncertain else "failed"
        db.commit()
        message = str(exc) if isinstance(exc, AsocAdapterError) else "Credential storage unavailable"
        raise HTTPException(status_code=exc.status_code if isinstance(exc, AsocAdapterError) else 503, detail={"delivery_id": delivery.id, "message": message}) from None
    delivery.state, delivery.remote_reference = receipt.state, receipt.reference
    db.commit()
    return {"delivery_id": delivery.id, "config_version": delivery.config_version, "state": delivery.state, "reference": delivery.remote_reference,
            "digest_sha256": delivery.digest_sha256, "processing_verified": False}


@router.get("/deliveries")
def deliveries(after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
               db: Session = Depends(get_db), actor: str = Depends(require_operator)):
    rows = db.query(AsocReportDelivery).filter(AsocReportDelivery.id > after).order_by(AsocReportDelivery.id).limit(limit).all()
    return {"deliveries": [{"id": row.id, "destination_id": row.destination_id,
            "provider": row.provider, "config_version": row.config_version, "target": json.loads(row.target_json),
            "state": row.state, "reference": row.remote_reference,
            "digest_sha256": row.digest_sha256} for row in rows]}
