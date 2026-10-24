import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_release_gate_is_one_documented_command_for_all_seed_profiles():
    package = json.loads(_read("package.json"))
    assert package["scripts"]["qa:release-gate"] == "./scripts/run_seeded_browser_gate.sh"

    runner = _read("scripts/run_seeded_browser_gate.sh")
    assert "for profile in empty kitchen-sink volume" in runner
    assert "--profile \"${profile}\"" in runner
    assert "--reset" in runner
    assert "QA_ROUTE_MANIFEST=\"${profile_dir}/manifest.json\"" in runner
    assert "QA_ARTIFACTS_DIR=\"${profile_dir}/browser\"" in runner

    docs = _read("qa/README.md")
    assert "npm run qa:release-gate" in docs
    for profile in ("empty", "kitchen-sink", "volume"):
        assert f"`{profile}`" in docs


def test_gate_consumes_merged_task326_mock_contract():
    runner = _read("scripts/run_seeded_browser_gate.sh")
    assert "docker-compose.integration-recovery.yml" in runner
    assert "qa_mock" in runner
    assert "/__control__/reset" in runner
    assert "http://qa_mock:18080" in runner
    assert 'SECPIPE_IMAGE="${SECPIPE_IMAGE:-scanops-demo:local}"' in runner
    assert "SCANOPS_MOCK_PORT" in runner
    assert "playwright install chromium" in runner
    assert "--wait-timeout 180" in runner
    overlay = _read("docker-compose.browser-gate.yml")
    assert "qa_mock:" in overlay
    assert "demo-network" in overlay
    assert (ROOT / "qa/mock_server.py").exists()
    assert (ROOT / "docker-compose.integration-recovery.yml").exists()

    from qa.mock_server import DUMMY_CREDENTIALS

    seed = _read("scripts/seed_demo_data.py")
    for credential in DUMMY_CREDENTIALS.values():
        assert credential in seed or credential == DUMMY_CREDENTIALS["ptai_jwt"]


def test_viewport_matrix_covers_supported_desktop_widths():
    config = _read("playwright.config.ts")
    qa_docs = _read("qa/README.md")
    readme = _read("README.md")
    signal_control_docs = _read("docs/signal-control.md")
    expected = {
        "{ name: 'desktop-1920', width: 1920, height: 1080 }",
        "{ name: 'desktop-1440', width: 1440, height: 1000 }",
    }
    assert all(item in config for item in expected)
    assert config.count("{ name: '") == 2
    assert "tablet" not in config.lower()
    assert "touch" not in config.lower()
    assert "Narrow-tablet and mobile layouts are outside the product" in qa_docs
    assert "desktop-ширинах 1920 и 1440 px" in readme
    assert "desktop-ширинах: 1920 и 1440 px" in signal_control_docs
    assert "1920/1440/1280/1024/768" not in readme
    assert "1920, 1440, 1280, 1024, 768" not in signal_control_docs


def test_overview_browser_contract_guards_the_two_column_composition():
    source = _read("qa/e2e/overview-layout.spec.ts")

    assert "Overview composition contract" in source
    assert "attention stays to the right of the river" in source
    assert "latency stays to the right of the chart" in source
    assert "telemetry begins inside the first viewport" in source
    assert "overview-first-viewport.png" in source


def test_release_reporter_fails_final_status_on_any_p0_or_p1():
    reporter = _read("qa/e2e/support/finding-reporter.ts")
    assert "severityCounts.P0 === 0 && severityCounts.P1 === 0" in reporter
    assert "async onEnd" in reporter
    assert "return { status: finalStatus }" in reporter
    assert "zero P0/P1 findings" in reporter


def test_layout_detector_has_false_positive_regression_fixture():
    detector = _read("qa/e2e/support/page-audit.ts")
    regression = _read("qa/e2e/detectors.spec.ts")
    assert "style.clip !== 'auto'" in detector
    assert "visibleTextGeometryOverflows" in detector
    assert "td.actions a[href]" in detector
    assert "a[data-action][href]" in detector
    assert "Execution context was destroyed" in detector
    assert "layout detector regressions" in regression
    assert "standalone-scanner-action" in regression
    assert "ordinary-inline-data-link" in regression


def test_interaction_matrix_covers_all_get_options_status_reload_and_back():
    interactions = _read("qa/e2e/interactions.spec.ts")
    navigation = _read("qa/e2e/support/navigation.ts")
    assert ".filter((option) => !option.disabled)" in interactions
    assert ".filter((option) => !option.selected" not in interactions
    assert "recordBadNavigation(reloadResponse" in interactions
    assert "recordBadNavigation(backResponse" in interactions
    assert "allowNullHistoryResponse" in interactions
    assert "page.reload" in interactions
    assert "page.goBack" in interactions
    assert "INTERACTION_HTTP_5XX" in navigation
    assert "INTERACTION_HTTP_4XX" in navigation
    assert "Product registry quarter filter Back and HTTP-error matrix" in interactions
    assert "for (const period of periods" in interactions
    assert "for (const mode of modes)" in interactions


def test_static_and_seeded_route_contracts_use_canonical_paths():
    routes = _read("qa/e2e/support/routes.ts")
    seed = _read("scripts/seed_demo_data.py")
    for path in (
        "/targets/products/{product_id}",
        "/targets/products/asoc/{asoc_id}",
        "/targets/repositories/{repository_id}",
        "/coverage/products/{product_id}",
        "/coverage/runs/{run_id}",
    ):
        assert path in routes

    for fragment in (
        'f"/targets/products/{first_product.id}"',
        'f"/targets/products/asoc/{first_asoc_card.asoc_id}"',
        'f"/targets/repositories/{first_repository.id}"',
        'f"/coverage/products/{first_product.id}"',
        'f"/coverage/runs/{first_audit.id}"',
    ):
        assert fragment in seed


def test_artifacts_default_outside_repository_and_local_fallbacks_are_ignored():
    runner = _read("scripts/run_seeded_browser_gate.sh")
    ignore = _read(".gitignore")
    assert '${TMPDIR:-/tmp}/scanops-browser-gate/' in runner
    assert "qa/artifacts/" in ignore
    assert "playwright-report/" in ignore
    assert "test-results/" in ignore
    docker_ignore = _read(".dockerignore")
    assert "node_modules/" in docker_ignore
    assert "qa/artifacts/" in docker_ignore
