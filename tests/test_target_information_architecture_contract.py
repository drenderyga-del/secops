import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "canonical-target-information-architecture.json"
ROUTER_PATHS = (
    ROOT / "app" / "routers" / "web" / "products.py",
    ROOT / "app" / "routers" / "web" / "logs.py",
    ROOT / "app" / "routers" / "web" / "audit.py",
    ROOT / "app" / "routers" / "web" / "config_scan.py",
    ROOT / "app" / "routers" / "web" / "iac.py",
    ROOT / "app" / "routers" / "web" / "next_ui.py",
    ROOT / "app" / "routers" / "web" / "reports.py",
)


def _contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _declared_get_routes():
    source = "\n".join(path.read_text(encoding="utf-8") for path in ROUTER_PATHS)
    return set(re.findall(r'@router\.get\(\s*"([^"]+)"', source))


def test_canonical_route_contract_has_one_owner_and_unique_names_and_paths():
    contract = _contract()
    routes = contract["canonical_routes"]

    assert contract["schema_version"] == 1
    assert len({route["name"] for route in routes}) == len(routes)
    assert len({(route["method"], route["path"]) for route in routes}) == len(routes)
    assert all(route["owner"] and route["purpose"] for route in routes)

    route_by_name = {route["name"]: route for route in routes}
    assert route_by_name["targets_collection"]["query"]["level"] == {
        "allowed": ["product", "repository"],
        "default": "product",
    }
    assert route_by_name["product_target_detail"]["path"] == "/targets/products/{product_id}"
    assert route_by_name["product_card_detail"]["path"] == "/targets/products/asoc/{asoc_id}"
    assert route_by_name["repository_target_360"]["path"] == "/targets/repositories/{repository_id}"
    assert route_by_name["coverage_aggregate"]["owner"] == "coverage"


def test_every_compatibility_route_has_an_explicit_non_circular_policy():
    compatibility = _contract()["compatibility_routes"]

    assert all(route["policy"] == "308_redirect" for route in compatibility)
    # A redirect either keeps the query verbatim or declares how it is translated.
    assert all(
        route["preserve_query"] is True or route.get("query_translation")
        for route in compatibility
    )
    assert all(route["from"] != route["to"].split("?", 1)[0] for route in compatibility)
    assert len({route["from"] for route in compatibility}) == len(compatibility)


def test_contract_accounts_for_current_canonical_and_retained_get_routes():
    declared = _declared_get_routes()
    contract = _contract()
    compatibility_sources = {route["from"] for route in contract["compatibility_routes"]}
    retained = {route["path"] for route in contract["retained_routes"]}

    expected_legacy_routes = {
        "/products",
        "/repositories",
        "/products/{product_id}",
        "/repos/{repository_id}",
        "/audit",
        "/audit/products/{product_id}",
        "/reports",
        "/reports/operator",
        "/classic/reports",
        "/reports/export.xlsx",
    }
    expected_retained_routes = {
        "/repos/{repository_id}/jobs",
        "/config-scan/products/{product_id}",
        "/iac/repositories/{repo_row_id}",
        "/targets/operator",
    }
    assert expected_legacy_routes <= declared
    assert compatibility_sources == expected_legacy_routes
    assert expected_retained_routes <= declared
    assert retained == expected_retained_routes


def test_repository_and_iac_detail_identifiers_are_deliberately_distinct():
    retained = {route["path"]: route for route in _contract()["retained_routes"]}

    target_route = next(
        route for route in _contract()["canonical_routes"] if route["name"] == "repository_target_360"
    )
    iac_route = retained["/iac/repositories/{repo_row_id}"]

    assert "{repository_id}" in target_route["path"]
    assert iac_route["path"] == "/iac/repositories/{repo_row_id}"
    assert iac_route["identifier_semantics"] == (
        "repo_row_id is IacRepository.id, not Repository.id."
    )
    assert "not a canonical Repository id" in iac_route["reason"]
