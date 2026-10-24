"""Config scan report parsing, tree building, and severity helpers (UI + API).

Kept separate from FastAPI routers to keep web.py thin and logic testable.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Product
from .models.config_scan import ConfigScanFinding, ConfigScanRun


def findings_severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for item in findings:
        severity = str(item.get("severity", "info")).lower()
        summary[severity] = summary.get(severity, 0) + 1
    return summary


def normalize_path_key(path: str) -> str:
    return (path or "").strip().strip("/").lower()


def load_report_json_file(path: str | Path) -> Optional[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def gitlab_path_from_product_field(path: Optional[str]) -> str:
    """Full GitLab path segment from product.path (may be a repo URL)."""
    raw = (path or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        from urllib.parse import urlparse

        raw = (urlparse(raw).path or "").strip()
    return raw.strip("/")


def scope_path_for_run(
    product: Product,
    run: Optional[ConfigScanRun],
    report: Optional[dict[str, Any]],
) -> str:
    md = (report.get("metadata") or {}) if report else {}
    gp = (md.get("group_path") or "").strip().strip("/")
    if gp:
        return gp
    if run:
        sp = (run.scope_path or "").strip().strip("/")
        if sp:
            return sp
    return gitlab_path_from_product_field(product.path) or (product.name or "").strip() or "unknown"


def find_project_blob_in_report(
    report: Optional[dict[str, Any]],
    project_query: str,
) -> Optional[dict[str, Any]]:
    if not report:
        return None
    pk = normalize_path_key(project_query)
    for proj in report.get("projects") or []:
        ppath = normalize_path_key(str(proj.get("project_path") or ""))
        if ppath == pk:
            return proj  # type: ignore[return-value]
    return None


def serialize_db_findings_as_project_blob(
    rows: list[Any],
) -> Optional[dict[str, Any]]:
    if not rows:
        return None
    findings: list[dict[str, Any]] = []
    for r in rows:
        findings.append(
            {
                "severity": r.severity,
                "code": r.rule_id,
                "title": r.title,
                "description": r.description,
                "recommendation": r.recommendation,
                "evidence": r.detail,
            }
        )
    return {
        "project_path": rows[0].project_path,
        "project_url": rows[0].project_url,
        "findings": findings,
        "findings_summary": findings_severity_counts(findings),
        "warnings_count": 0,
    }


def fetch_findings_blob_from_db(
    db: Session,
    run_id: int,
    project_query: str,
) -> Optional[dict[str, Any]]:
    pk = normalize_path_key(project_query)
    q_rows = (
        db.query(ConfigScanFinding)
        .filter(ConfigScanFinding.run_id == run_id)
        .filter(func.lower(ConfigScanFinding.project_path) == pk)
        .order_by(ConfigScanFinding.id)
        .all()
    )
    return serialize_db_findings_as_project_blob(q_rows)


def resolve_project_findings_blob(
    db: Session,
    run: ConfigScanRun,
    report: Optional[dict[str, Any]],
    project_query: str,
) -> Optional[dict[str, Any]]:
    blob = find_project_blob_in_report(report, project_query)
    if blob is not None:
        findings = blob.get("findings") or []
        if "findings_summary" not in blob or not blob.get("findings_summary"):
            blob = {**blob, "findings_summary": findings_severity_counts(findings)}
        return blob
    return fetch_findings_blob_from_db(db, run.id, project_query)


def relative_path_under_scope(project_path: str, scope_path: str) -> str:
    pp = [p for p in project_path.strip().strip("/").split("/") if p]
    sp = [p for p in scope_path.strip().strip("/").split("/") if p]
    if not sp:
        return "/".join(pp)
    if len(pp) < len(sp):
        return "/".join(pp)
    if [x.lower() for x in pp[: len(sp)]] != [x.lower() for x in sp]:
        return "/".join(pp)
    return "/".join(pp[len(sp) :])


def build_project_tree(projects: list[dict[str, Any]], scope_path: str) -> dict[str, Any]:
    normalized_scope = scope_path.strip("/")
    root: dict[str, Any] = {"name": ".", "path": ".", "_children": {}, "repos": []}

    for project in projects:
        project_path = str(project.get("project_path") or "")
        if not project_path:
            continue

        relative = relative_path_under_scope(project_path, normalized_scope)

        parts = [part for part in relative.split("/") if part]
        if not parts:
            # When scope_path equals the repository path exactly (common for
            # one-repo/manual scans), keep the repo as a root entry instead of dropping it.
            original_parts = [part for part in project_path.strip().strip("/").split("/") if part]
            if not original_parts:
                continue
            parts = [original_parts[-1]]

        folder_parts = parts[:-1]
        findings = project.get("findings", []) or []
        warnings = project.get("warnings", []) or []
        fsum = project.get("findings_summary") or findings_severity_counts(findings)

        node = root
        path_stack: list[str] = []
        for part in folder_parts:
            path_stack.append(part)
            children = node["_children"]
            if part not in children:
                children[part] = {"name": part, "path": "/".join(path_stack), "_children": {}, "repos": []}
            node = children[part]

        node["repos"].append({
            "repo_name": parts[-1],
            "project_path": project_path,
            "project_url": project.get("project_url"),
            "relative_path": relative,
            "findings_run_id": project.get("findings_run_id"),
            "findings": findings,
            "findings_count": len(findings),
            "findings_summary": fsum,
            "warnings_count": len(warnings),
        })

    def finalize_tree(node: dict[str, Any]) -> dict[str, Any]:
        children_map = node.get("_children", {})
        children = [finalize_tree(children_map[name]) for name in sorted(children_map)]
        repos = sorted(
            node.get("repos", []),
            key=lambda item: (item["findings_count"], item["repo_name"]),
            reverse=True,
        )
        findings_total = sum(int(item["findings_count"]) for item in repos)
        repos_count = len(repos)
        for child in children:
            findings_total += int(child["findings_total"])
            repos_count += int(child["repos_count"])
        return {
            "name": node["name"],
            "path": node["path"],
            "children": children,
            "repos": repos,
            "repos_count": repos_count,
            "findings_total": findings_total,
        }

    return finalize_tree(root)


def flat_repo_rows_from_projects(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for project in projects:
        project_path = str(project.get("project_path") or "")
        if not project_path:
            continue
        findings = project.get("findings", []) or []
        warnings = project.get("warnings", []) or []
        fsum = project.get("findings_summary") or findings_severity_counts(findings)
        rows.append(
            {
                "project_path": project_path,
                "project_url": project.get("project_url"),
                "findings_run_id": project.get("findings_run_id"),
                "findings": findings,
                "findings_count": len(findings),
                "findings_summary": fsum,
                "warnings_count": len(warnings),
            }
        )
    rows.sort(key=lambda r: (r["findings_count"], r["project_path"]), reverse=True)
    return rows


def group_repo_rows_by_first_segment(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        parts = [p for p in str(r.get("project_path") or "").split("/") if p]
        key = parts[0] if parts else "_other"
        buckets[key].append(r)
    groups: list[dict[str, Any]] = []
    for name in buckets:
        rs = sorted(
            buckets[name],
            key=lambda x: (-x["findings_count"], x["project_path"]),
        )
        ft = sum(x["findings_count"] for x in rs)
        groups.append(
            {
                "name": name,
                "repos": rs,
                "repos_count": len(rs),
                "findings_total": ft,
            }
        )
    groups.sort(key=lambda g: (-g["findings_total"], g["name"]))
    return groups


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 9}


def collect_top_findings_from_projects(
    raw_projects: list[dict[str, Any]],
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for proj in raw_projects:
        ppath = proj.get("project_path")
        purl = proj.get("project_url")
        for item in proj.get("findings", []) or []:
            out.append(
                {
                    "project_path": ppath,
                    "project_url": purl,
                    **item,
                }
            )
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    out.sort(key=lambda x: _SEVERITY_ORDER.get(str(x.get("severity", "")).lower(), 9))
    return out[:limit]


def format_scan_duration(started_at: Any, finished_at: Any) -> str:
    if started_at is None:
        return "-"

    def to_utc_naive(value: Any) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    start = to_utc_naive(started_at)
    end = to_utc_naive(finished_at) if finished_at is not None else datetime.utcnow()
    seconds = int(max((end - start).total_seconds(), 0))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
