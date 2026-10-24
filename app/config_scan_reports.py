from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
REPORTS_DIR = Path(os.getenv("CONFIG_SCAN_REPORTS_DIR", "./config_scan_reports"))

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def write_report_files(
    report: dict[str, Any],
    run_id: int,
) -> tuple[str, str, str, int]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    json_path = REPORTS_DIR / f"run-{run_id}.json"
    html_path = REPORTS_DIR / f"run-{run_id}.html"

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    html_path.write_text(render_html_report(report), encoding="utf-8")

    summary_json = json.dumps(report.get("summary", {}), ensure_ascii=False)
    findings_count = int(report.get("summary", {}).get("findings_total", 0))
    return str(json_path), str(html_path), summary_json, findings_count


def render_html_report(report: dict[str, Any]) -> str:
    template = _env.get_template("config_scan_report_export.html")
    return template.render(report=report)
