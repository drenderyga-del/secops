"""Canonical URL builders for Target and Coverage navigation."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

from fastapi import Request


TARGETS_PATH = "/targets"
PRODUCT_TARGET_PATH = "/targets/products/{product_id}"
REPOSITORY_TARGET_PATH = "/targets/repositories/{repository_id}"
COVERAGE_PATH = "/coverage"
PRODUCT_COVERAGE_PATH = "/coverage/products/{product_id}"
COVERAGE_RUNS_PATH = "/coverage/runs"
COVERAGE_RUN_PATH = "/coverage/runs/{run_id}"

TARGET_LEVELS = frozenset({"product", "repository"})
DEFAULT_TARGET_LEVEL = "product"


def targets_url(level: str = DEFAULT_TARGET_LEVEL) -> str:
    normalized = level if level in TARGET_LEVELS else DEFAULT_TARGET_LEVEL
    return f"{TARGETS_PATH}?{urlencode({'level': normalized})}"


def product_target_url(product_id: int) -> str:
    return PRODUCT_TARGET_PATH.format(product_id=product_id)


def repository_target_url(repository_id: int) -> str:
    return REPOSITORY_TARGET_PATH.format(repository_id=repository_id)


def product_coverage_url(product_id: int) -> str:
    return PRODUCT_COVERAGE_PATH.format(product_id=product_id)


def coverage_run_url(run_id: int) -> str:
    return COVERAGE_RUN_PATH.format(run_id=run_id)


def compatibility_redirect_url(
    request: Request,
    destination: str,
    *,
    forced_query: Mapping[str, str] | None = None,
) -> str:
    """Preserve a legacy deep link while letting canonical values win."""

    forced = dict(forced_query or {})
    query_items = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in forced
    ]
    query_items.extend(forced.items())
    query = urlencode(query_items, doseq=True)
    return f"{destination}?{query}" if query else destination
