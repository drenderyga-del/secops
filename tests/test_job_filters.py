from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sast_category_includes_ptai_scan_variants():
    source = (ROOT / "app/database.py").read_text()
    assert '["ptai_scan", "ptai_async_scan", "semgrep_scan"]' in source
