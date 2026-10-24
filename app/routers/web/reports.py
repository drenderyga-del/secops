"""Retired ASOC report routes.

Quarterly product reporting now lives on the Products registry
(``/targets?level=product``) and its XLSX export. These routes only keep old
bookmarks working: ``year`` + ``quarter`` become the equivalent
"new in quarter" period.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ...product_registry import Quarter

router = APIRouter(tags=["web"])


def legacy_report_period(request: Request) -> dict[str, str]:
    year = (request.query_params.get("year") or "").strip()
    quarter = (request.query_params.get("quarter") or "").strip()
    if not year or not quarter:
        return {}
    # Validate exactly as the registry does; anything else opens the full registry.
    try:
        period = Quarter.parse(f"{year}-Q{quarter}")
    except ValueError:
        return {}
    return {"period": period.key, "mode": "new"}


def products_registry_url(request: Request) -> str:
    return "/targets?" + urlencode({"level": "product", **legacy_report_period(request)})


@router.get("/classic/reports", include_in_schema=False)
@router.get("/reports/operator", include_in_schema=False, name="reports_operator")
async def reports_operator_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(products_registry_url(request), status_code=308)


@router.get("/reports/export.xlsx", include_in_schema=False)
async def reports_export_redirect(request: Request) -> RedirectResponse:
    query = urlencode(legacy_report_period(request))
    return RedirectResponse(
        "/api/ui/products/export.xlsx" + (f"?{query}" if query else ""),
        status_code=308,
    )
