from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_integration_radar_uses_one_centered_geometry_contract():
    source = (ROOT / "frontend" / "src" / "App.tsx").read_text()
    styles = (ROOT / "frontend" / "src" / "globals.css").read_text()
    extended = (ROOT / "frontend" / "src" / "extended.css").read_text()
    sweep_rule_match = re.search(r"\.radar-sweep \{([^}]+)\}", styles)
    assert sweep_rule_match is not None
    sweep_rule = sweep_rule_match.group(1)
    sweep_keyframes = next(
        line
        for line in styles.splitlines()
        if line.startswith("@keyframes radarSweep ")
    )

    assert 'className="radar-stage"' in source
    assert ".radar-stage" in styles
    assert "width: 62%" in sweep_rule
    assert "transform: translate(-50%, -50%)" in sweep_rule
    assert "transform-origin: 50% 50%" in sweep_rule
    assert "conic-gradient(from 67.5deg at 50% 50%" in sweep_rule
    assert "transparent 45deg" in sweep_rule
    assert "clip-path:" not in sweep_rule
    assert "translate(-50%, -50%) rotate(0deg)" in sweep_keyframes
    assert "translate(-50%, -50%) rotate(360deg)" in sweep_keyframes
    assert "@keyframes radarOrbitSpin" in styles
    assert "width: max-content" in extended
    assert "max-width: 48%" in extended
    assert "left: 82%" in extended
    assert "left: 18%" in extended


def test_logs_metrics_section_is_conditional_without_a_duplicate_tab():
    source = (ROOT / "app/templates/logs.html").read_text()
    assert "{% if duration_summary or job_duration_summary %}" in source
    assert '<section id="metrics" class="page-section stats-grid duration-grid">' in source
    assert "local_nav.toc(" not in source


def test_config_scan_run_findings_section_is_conditional_without_a_duplicate_tab():
    source = (ROOT / "app/templates/config_scan_run.html").read_text()
    assert "{% if top_findings %}" in source
    assert '<section id="findings" class="page-section cscan-tree-section">' in source
    assert "local_nav.toc(" not in source
