from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

IAC_MARKER_PATTERNS = (
    ("terraform", re.compile(r"(^|/)[^/]+\.(tf|tfvars)$", re.IGNORECASE)),
    ("helm", re.compile(r"(^|/)Chart\.yaml$", re.IGNORECASE)),
    ("helm", re.compile(r"(^|/)values[^/]*\.ya?ml$", re.IGNORECASE)),
    ("kubernetes", re.compile(r"(^|/)kustomization\.ya?ml$", re.IGNORECASE)),
    ("dockerfile", re.compile(r"(^|/)(Dockerfile|[^/]+\.Dockerfile)$", re.IGNORECASE)),
    ("ansible", re.compile(r"(^|/)(playbook|site|main)\.ya?ml$", re.IGNORECASE)),
    ("ansible", re.compile(r"(^|/)roles/[^/]+/(tasks|handlers|defaults|vars)/.+\.ya?ml$", re.IGNORECASE)),
    ("cloudformation", re.compile(r"(^|/).*(cloudformation|cfn|sam).*\.(ya?ml|json)$", re.IGNORECASE)),
)


def detect_iac_markers(tree_items: list[dict[str, Any]]) -> list[str]:
    markers: set[str] = set()
    for item in tree_items:
        if str(item.get("type") or "blob") != "blob":
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        if not path:
            continue
        if path == ".gitlab-ci.yml":
            continue
        for marker, pattern in IAC_MARKER_PATTERNS:
            if pattern.search(path):
                markers.add(marker)
    return sorted(markers)


def _severity_from_sarif(level: str | None, properties: dict[str, Any]) -> str:
    raw = (
        properties.get("security-severity")
        or properties.get("severity")
        or properties.get("problem.severity")
        or level
        or "info"
    )
    value = str(raw).strip().lower()
    if value.replace(".", "", 1).isdigit():
        score = float(value)
        if score >= 9:
            return "critical"
        if score >= 7:
            return "high"
        if score >= 4:
            return "medium"
        if score > 0:
            return "low"
        return "info"
    if value in {"error", "critical"}:
        return "critical"
    if value in {"warning", "high"}:
        return "high"
    if value in {"medium", "moderate"}:
        return "medium"
    if value in {"low", "note"}:
        return "low"
    return "info"


def parse_checkov_sarif(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != "2.1.0" or not isinstance(data.get("runs"), list):
        raise ValueError("Invalid SARIF schema: expected version 2.1.0 and a runs array")
    findings: list[dict[str, Any]] = []
    for run in data["runs"]:
        rules = {
            str(rule.get("id")): rule
            for tool in [run.get("tool") or {}]
            for driver in [tool.get("driver") or {}]
            for rule in driver.get("rules") or []
            if rule.get("id")
        }
        for result in run.get("results") or []:
            rule_id = str(result.get("ruleId") or result.get("rule", {}).get("id") or "UNKNOWN")
            rule = rules.get(rule_id) or {}
            props = {}
            props.update(rule.get("properties") or {})
            props.update(result.get("properties") or {})
            message = result.get("message") or {}
            title = (
                rule.get("shortDescription", {}).get("text")
                or rule.get("name")
                or message.get("text")
                or rule_id
            )
            description = (
                rule.get("fullDescription", {}).get("text")
                or rule.get("help", {}).get("text")
                or message.get("text")
            )
            recommendation = rule.get("help", {}).get("markdown") or rule.get("help", {}).get("text")
            location = ((result.get("locations") or [{}])[0].get("physicalLocation") or {})
            artifact = location.get("artifactLocation") or {}
            region = location.get("region") or {}
            findings.append(
                {
                    "severity": _severity_from_sarif(result.get("level"), props),
                    "rule_id": rule_id,
                    "title": str(title),
                    "description": description,
                    "recommendation": recommendation,
                    "file_path": artifact.get("uri"),
                    "line": region.get("startLine"),
                    "evidence": message.get("text"),
                }
            )
    return findings


def checkov_command(output_path: str) -> list[str]:
    return [
        os.getenv("IAC_CHECKOV_BIN", "checkov"),
        "--directory",
        ".",
        "--framework",
        "cloudformation",
        "--framework",
        "terraform",
        "--framework",
        "kubernetes",
        "--framework",
        "dockerfile",
        "--framework",
        "helm",
        "--framework",
        "ansible",
        "--framework",
        "gitlab_ci",
        "--output",
        "sarif",
        "--quiet",
        "--soft-fail",
        "--output-file-path",
        output_path,
    ]
