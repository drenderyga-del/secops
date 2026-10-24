"""Web UI routes, split by area. External contract preserved:
``from app.routers.web import router`` (and a couple of helpers used by tests).
"""
from fastapi import APIRouter

from .common import templates, get_db, _normalize_product_group_path  # re-exported
from . import audit, config_scan, dashboard, iac, integrations, logs, next_ui, pipelines, product_registry, products, ptai, reports, scanners, signal_control

router = APIRouter(tags=["web"])
# Canonical product-page GETs belong to ``next_ui``. The remaining modules may
# add operator, detail, compatibility, and mutation routes, but must not reclaim
# the same method/path pairs (enforced by test_route_ownership_contract.py).
for module in (dashboard, next_ui, signal_control, product_registry, pipelines, products, logs, audit, iac, config_scan, ptai, scanners, integrations, reports):
    router.include_router(module.router)
