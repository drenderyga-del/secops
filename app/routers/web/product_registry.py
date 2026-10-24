"""Product registry API: ASOC products, their ScanOps sync state and exports."""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ...asoc_products import (
    asoc_sync_interval_minutes,
    asoc_sync_metadata,
    read_sync_outcome,
)
from ...display_time import DISPLAY_TIMEZONE_NAME
from ...integration_settings import connector_snapshot, runtime_setting
from ...product_registry import (
    RegistryQuery,
    build_registry,
    export_filename,
    export_xlsx,
    product_detail,
)
from .common import get_db

router = APIRouter(tags=["web"])


def _registry_query(
    period: Optional[str],
    mode: Optional[str],
    scope: Optional[str],
    status: Optional[str],
    q: Optional[str],
) -> RegistryQuery:
    try:
        return RegistryQuery.from_params(period=period, mode=mode, scope=scope, status=status, q=q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _sync_state(db: Session) -> dict:
    metadata = asoc_sync_metadata(
        db,
        configured=bool(connector_snapshot(db, "asoc").get("configured")),
    )
    last_synced = metadata["last_synced"]
    return {
        "state": metadata["state"],
        "configured": metadata["configured"],
        "last_synced": last_synced.isoformat() if last_synced else None,
        "total_cards": metadata["total_cards"],
        "removed_cards": metadata["historical_cards"],
        "interval_minutes": asoc_sync_interval_minutes(),
        "last_attempt": read_sync_outcome(db),
    }


@router.get("/api/ui/products", response_class=JSONResponse)
def product_registry(
    period: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    scope: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = _registry_query(period, mode, scope, status, q)
    registry = build_registry(db, query, asoc_base_url=runtime_setting("HEXWAY_URL", "", db=db))
    params = urlencode(query.as_params())
    return {
        **registry,
        "sync": _sync_state(db),
        "timezone": DISPLAY_TIMEZONE_NAME,
        "export_href": "/api/ui/products/export.xlsx" + (f"?{params}" if params else ""),
    }


@router.get("/api/ui/products/export.xlsx")
def product_registry_export(
    period: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    scope: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = _registry_query(period, mode, scope, status, q)
    registry = build_registry(db, query, asoc_base_url=runtime_setting("HEXWAY_URL", "", db=db))
    return Response(
        content=export_xlsx(registry),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{export_filename(registry)}"'},
    )


@router.get("/api/ui/products/widgets/{asoc_id:path}", response_class=JSONResponse)
def product_card_detail_api(asoc_id: str, db: Session = Depends(get_db)):
    detail = product_detail(
        db,
        asoc_id=asoc_id,
        asoc_base_url=runtime_setting("HEXWAY_URL", "", db=db),
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="ASOC product not found")
    return {**detail, "sync": _sync_state(db), "timezone": DISPLAY_TIMEZONE_NAME}


@router.get("/api/ui/products/targets/{product_id}", response_class=JSONResponse)
def product_target_detail_api(product_id: int, db: Session = Depends(get_db)):
    detail = product_detail(
        db,
        product_id=product_id,
        asoc_base_url=runtime_setting("HEXWAY_URL", "", db=db),
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return {**detail, "sync": _sync_state(db), "timezone": DISPLAY_TIMEZONE_NAME}
