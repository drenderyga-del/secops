from collections import defaultdict
from urllib.parse import parse_qsl, urlsplit

from fastapi.testclient import TestClient

from app.main import app


CANONICAL_REACT_ROUTES = {
    "/pipelines": "active_pipelines",
    "/targets": "targets_collection",
    "/coverage": "coverage_aggregate",
    "/iac": "iac_dashboard",
    "/config-scan": "config_scan_overview",
    "/ptai-scans": "ptai_scans",
    "/integrations": "integrations",
}

OPERATOR_ROUTES = {
    "/pipelines/operator",
    "/targets/operator",
    "/coverage/operator",
    "/iac/operator",
    "/config-scan/operator",
    "/ptai-scans/operator",
    "/integrations/operator",
}


def _leaf_routes(routes):
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _leaf_routes(original_router.routes)
            continue
        if getattr(route, "path", None) and getattr(route, "methods", None):
            yield route


def test_assembled_app_has_one_get_owner_per_path():
    owners = defaultdict(list)
    for route in _leaf_routes(app.router.routes):
        if "GET" in route.methods:
            owners[route.path].append(route)

    duplicates = {
        path: [f"{route.endpoint.__module__}.{route.endpoint.__name__}" for route in routes]
        for path, routes in owners.items()
        if len(routes) > 1
    }
    assert duplicates == {}

    for path, expected_name in CANONICAL_REACT_ROUTES.items():
        route = owners[path][0]
        assert route.endpoint.__module__ == "app.routers.web.next_ui"
        assert route.name == expected_name
        assert route.include_in_schema is False


def test_canonical_pages_are_runtime_shells_and_only_operators_are_in_openapi():
    client = TestClient(app)
    for path in CANONICAL_REACT_ROUTES:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200, path
        assert '<div id="root"></div>' in response.text, path

    schema_paths = app.openapi()["paths"]
    for path in CANONICAL_REACT_ROUTES:
        assert "get" not in schema_paths.get(path, {})
    for path in OPERATOR_ROUTES:
        assert "get" in schema_paths[path]


def test_retired_next_duration_view_redirects_to_its_canonical_route():
    response = TestClient(app).get(
        "/next?view=duration&timeframe=30d&tool=sast",
        follow_redirects=False,
    )

    location = urlsplit(response.headers["location"])
    assert response.status_code == 308
    assert location.path == "/dashboard/duration"
    assert parse_qsl(location.query) == [("timeframe", "30d"), ("tool", "sast")]
