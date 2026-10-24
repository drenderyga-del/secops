from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.routers.web import audit, logs, products
from app.target_navigation import (
    COVERAGE_PATH,
    PRODUCT_COVERAGE_PATH,
    PRODUCT_TARGET_PATH,
    REPOSITORY_TARGET_PATH,
    TARGETS_PATH,
)


ROOT = Path(__file__).resolve().parents[1]


def _router_paths():
    def collect(router):
        paths = set()
        for route in router.routes:
            if hasattr(route, "original_router"):
                paths.update(collect(route.original_router))
            elif hasattr(route, "path"):
                paths.add(route.path)
        return paths

    return collect(app.router)


def _redirect_client():
    test_app = FastAPI()
    test_app.include_router(products.router)
    test_app.include_router(logs.router)
    test_app.include_router(audit.router)
    return TestClient(test_app)


def test_canonical_target_and_coverage_routes_are_registered():
    paths = _router_paths()

    assert {
        TARGETS_PATH,
        PRODUCT_TARGET_PATH,
        REPOSITORY_TARGET_PATH,
        COVERAGE_PATH,
        PRODUCT_COVERAGE_PATH,
    } <= paths


def test_legacy_collection_routes_redirect_and_canonical_level_wins():
    with _redirect_client() as client:
        product = client.get(
            "/products?foo=one&foo=two&level=repository",
            follow_redirects=False,
        )
        repository = client.get(
            "/repositories?sort=name",
            follow_redirects=False,
        )

    assert product.status_code == 308
    product_location = urlsplit(product.headers["location"])
    assert product_location.path == TARGETS_PATH
    assert parse_qsl(product_location.query) == [
        ("foo", "one"),
        ("foo", "two"),
        ("level", "product"),
    ]

    assert repository.status_code == 308
    repository_location = urlsplit(repository.headers["location"])
    assert repository_location.path == TARGETS_PATH
    assert parse_qsl(repository_location.query) == [
        ("sort", "name"),
        ("level", "repository"),
    ]


def test_legacy_detail_routes_redirect_without_losing_query_state():
    cases = (
        ("/products/42?tab=posture", "/targets/products/42", [("tab", "posture")]),
        (
            "/repos/9?source=iac_repository",
            "/targets/repositories/9",
            [("source", "iac_repository")],
        ),
        (
            "/audit?status_filter=inactive",
            "/coverage/operator",
            [("status_filter", "inactive")],
        ),
        (
            "/audit/products/42?only_problems=true",
            "/coverage/products/42",
            [("only_problems", "true")],
        ),
    )

    with _redirect_client() as client:
        responses = [
            client.get(source, follow_redirects=False)
            for source, _, _ in cases
        ]

    for response, (_, expected_path, expected_query) in zip(responses, cases):
        assert response.status_code == 308
        location = urlsplit(response.headers["location"])
        assert location.path == expected_path
        assert parse_qsl(location.query) == expected_query


def test_unfiltered_audit_alias_uses_canonical_coverage_workspace():
    with _redirect_client() as client:
        response = client.get("/audit", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == COVERAGE_PATH


def test_filtered_audit_alias_uses_operator_contract_without_losing_state():
    query = (
        "only_problems=true"
        "&selected_product_ids=4%2C9"
        "&status=inactive"
        "&status_filter=not_configured"
        "&run_id=17"
    )
    with _redirect_client() as client:
        response = client.get(f"/audit?{query}", follow_redirects=False)

    location = urlsplit(response.headers["location"])
    assert response.status_code == 308
    assert location.path == "/coverage/operator"
    assert parse_qsl(location.query) == [
        ("only_problems", "true"),
        ("selected_product_ids", "4,9"),
        ("status", "inactive"),
        ("status_filter", "not_configured"),
        ("run_id", "17"),
    ]


@pytest.mark.needs_db
def test_filtered_canonical_coverage_bookmark_uses_operator_contract():
    query = (
        "only_problems=true"
        "&selected_product_ids=4%2C9"
        "&status=inactive"
        "&status_filter=not_configured"
        "&scan_started=true"
        "&run_id=17"
    )

    with TestClient(app) as client:
        response = client.get(f"/coverage?{query}", follow_redirects=False)

    location = urlsplit(response.headers["location"])
    assert response.status_code == 308
    assert location.path == "/coverage/operator"
    assert parse_qsl(location.query) == [
        ("only_problems", "true"),
        ("selected_product_ids", "4,9"),
        ("status", "inactive"),
        ("status_filter", "not_configured"),
        ("scan_started", "true"),
        ("run_id", "17"),
    ]


@pytest.mark.needs_db
def test_unfiltered_canonical_coverage_stays_in_signal_control():
    with TestClient(app) as client:
        response = client.get("/coverage", follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_unknown_entities_keep_404_at_canonical_handlers(monkeypatch):
    class MissingQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    missing_db = SimpleNamespace(query=lambda *_args: MissingQuery())

    class MissingTarget360:
        def __init__(self, _db):
            pass

        def snapshot(self, *_args, **_kwargs):
            raise LookupError("missing")

    monkeypatch.setattr(logs, "Target360Service", MissingTarget360)
    test_app = FastAPI()
    test_app.include_router(products.router)
    test_app.include_router(logs.router)
    test_app.include_router(audit.router)
    test_app.dependency_overrides[products.get_db] = lambda: missing_db
    test_app.dependency_overrides[logs.get_db] = lambda: missing_db
    test_app.dependency_overrides[audit.get_db] = lambda: missing_db

    with TestClient(test_app) as client:
        product = client.get("/targets/products/404")
        repository = client.get("/targets/repositories/404")
        coverage = client.get("/coverage/products/404")

    assert product.status_code == 404
    assert repository.status_code == 404
    assert coverage.status_code == 404


def test_first_party_templates_do_not_link_to_legacy_get_destinations():
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "app" / "templates").rglob("*.html")
    )

    assert 'href="/products' not in templates
    assert 'href="/repositories' not in templates
    assert 'href="/audit' not in templates
    assert 'href="/repos/{{ repository.id }}?' not in templates
    assert 'href="/repos/{{ repo.id }}"' not in templates

    # Retained mutation and Operations evidence routes are not entity links.
    assert 'action="/products/' in templates
    assert 'action="/audit/' in templates
    assert 'href="/repos/{{ repository.id }}/jobs"' in templates


def test_coverage_templates_drill_into_canonical_targets():
    coverage = (ROOT / "app" / "templates" / "audit.html").read_text(encoding="utf-8")
    coverage_detail = (
        ROOT / "app" / "templates" / "audit_product_detail.html"
    ).read_text(encoding="utf-8")

    assert "product_coverage_url(card.product.id)" in coverage
    assert "product_target_url(card.product.id)" in coverage
    assert "repository_target_url(row.target_repository.id)" in coverage_detail
    assert "Open Target 360" in coverage_detail
