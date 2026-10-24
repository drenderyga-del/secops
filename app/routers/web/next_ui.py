"""Canonical entrypoints for the Signal Control React presentation layer."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from .reports import legacy_report_period, products_registry_url

router = APIRouter()

_INDEX = Path("app/static/next/index.html")

_VIEW_ROUTES = {
    "overview": "/",
    "duration": "/dashboard/duration",
    "executions": "/executions",
    "jobs": "/logs",
    "pipelines": "/pipelines",
    "config": "/config-scan",
    "iac": "/iac",
    "ptai": "/ptai-scans",
    "failed": "/logs",
    "coverage": "/coverage",
    "products": "/targets",
    "repositories": "/targets",
    "reports": "/targets",
    "scanners": "/scanners",
    "integrations": "/integrations",
}

_COVERAGE_OPERATOR_QUERY_KEYS = {
    "only_problems",
    "selected_product_ids",
    "status",
    "status_filter",
    "scan_started",
    "run_id",
}


def _index() -> FileResponse:
    return FileResponse(_INDEX, media_type="text/html")


@router.get("/next", include_in_schema=False)
async def signal_control_ui(
    request: Request,
    view: str = Query("overview"),
):
    """Permanently redirect the retired preview namespace to product URLs."""
    destination = _VIEW_ROUTES.get(view, "/")
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "view"
    ]
    if view == "failed":
        params = [(key, value) for key, value in params if key != "status"]
        params.append(("status", "failed"))
    elif view == "products":
        params = [(key, value) for key, value in params if key != "level"]
        params.append(("level", "product"))
    elif view == "repositories":
        params = [(key, value) for key, value in params if key != "level"]
        params.append(("level", "repository"))
    elif view == "reports":
        # The retired report's filters map onto the Products quarter slice only.
        params = [("level", "product"), *legacy_report_period(request).items()]
    query = f"?{urlencode(params, doseq=True)}" if params else ""
    return RedirectResponse(url=f"{destination}{query}", status_code=308)


@router.get("/executions", include_in_schema=False)
async def signal_control_executions() -> FileResponse:
    """Serve the durable execution lifecycle at its canonical route."""
    return _index()


@router.get("/pipelines", include_in_schema=False, name="active_pipelines")
@router.get("/config-scan", include_in_schema=False, name="config_scan_overview")
@router.get("/iac", include_in_schema=False, name="iac_dashboard")
@router.get("/ptai-scans", include_in_schema=False, name="ptai_scans")
@router.get("/scanners", include_in_schema=False)
@router.get("/integrations", include_in_schema=False, name="integrations")
async def signal_control_workspace() -> FileResponse:
    """Serve every primary product workspace from its durable URL."""
    return _index()


@router.get("/pipelines/{active_pipeline_id}", include_in_schema=False)
async def signal_control_pipeline_detail(active_pipeline_id: int) -> FileResponse:
    """Serve the pipeline detail shell for an internal ActivePipeline id."""
    return _index()


@router.get("/coverage", include_in_schema=False, name="coverage_aggregate")
async def signal_control_coverage(
    request: Request,
) -> Response:
    """Keep retained operator filters reachable from canonical bookmarks."""
    if any(key in request.query_params for key in _COVERAGE_OPERATOR_QUERY_KEYS):
        query = urlencode(request.query_params.multi_items(), doseq=True)
        return RedirectResponse(
            url=f"/coverage/operator?{query}",
            status_code=308,
        )
    return _index()


@router.get("/reports", include_in_schema=False, name="reports_page")
async def signal_control_reports(request: Request) -> RedirectResponse:
    """Quarterly product reporting lives on the Products registry."""
    return RedirectResponse(products_registry_url(request), status_code=308)


@router.get("/targets", include_in_schema=False, name="targets_collection")
async def signal_control_targets() -> FileResponse:
    """Serve product or repository inventory according to the level query."""
    return _index()


@router.get("/targets/products/{product_id}", include_in_schema=False, name="product_target_detail")
async def signal_control_product_target(product_id: int) -> FileResponse:
    """Serve the product drill-down for a monitored GitLab product group."""
    return _index()


@router.get("/targets/products/asoc/{asoc_id:path}", include_in_schema=False, name="product_card_detail")
async def signal_control_product_card(asoc_id: str) -> FileResponse:
    """Serve the product drill-down for a canonical ASOC product card."""
    return _index()


@router.get("/", include_in_schema=False)
async def signal_control_overview() -> FileResponse:
    """Serve Signal Control Overview as the product home."""
    return _index()
