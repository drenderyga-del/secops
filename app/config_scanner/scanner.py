from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import yaml

from .gitlab_client import GitLabAPIError, GitLabClient
from .rules import (
    analyze_ci_configuration,
    analyze_project_settings,
    finding,
    sort_findings,
    summarize_findings,
)

ProgressCallback = Callable[[int, int], Awaitable[None] | None]


class GitLabScanner:
    def __init__(
        self,
        gitlab_url: str,
        token: str,
        group_path: str,
        *,
        verify_ssl: bool = True,
        include_subgroups: bool = True,
        progress_callback: ProgressCallback | None = None,
        project_scan_concurrency: int = 12,
        request_timeout: int = 30,
        max_include_depth: int = 10,
    ) -> None:
        self.gitlab_url = gitlab_url
        self.token = token
        self.group_path = (group_path or "").strip().strip("/")
        self.verify_ssl = verify_ssl
        self.include_subgroups = include_subgroups
        self.progress_callback = progress_callback
        self.project_scan_concurrency = max(project_scan_concurrency, 1)
        self.request_timeout = request_timeout
        self.max_include_depth = max_include_depth
        self._project_default_branch_cache: dict[str, str] = {}

    async def scan(self) -> dict[str, Any]:
        async with GitLabClient(
            self.gitlab_url,
            self.token,
            verify_ssl=self.verify_ssl,
            request_timeout=self.request_timeout,
        ) as client:
            project_list, scope_type = await self._resolve_scope_projects(client)
            total_projects = len(project_list)

            semaphore = asyncio.Semaphore(self.project_scan_concurrency)
            completed = 0
            project_results: list[dict[str, Any]] = []

            async def scan_one(project: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    return await self._scan_project(client, project)

            tasks = [asyncio.create_task(scan_one(project)) for project in project_list]

            for task in asyncio.as_completed(tasks):
                result = await task
                project_results.append(result)
                completed += 1
                if self.progress_callback:
                    maybe_coro = self.progress_callback(total_projects, completed)
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro

        summary = self._build_summary(project_results, total_projects)
        return {
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "gitlab_url": self.gitlab_url,
                "group_path": self.group_path,
                "scope_type": scope_type,
            },
            "summary": summary,
            "projects": sorted(
                project_results,
                key=lambda item: (len(item.get("findings", [])), item.get("project_path", "")),
                reverse=True,
            ),
        }

    @staticmethod
    def _project_under_scope(project: dict[str, Any], scope_norm_lower: str) -> bool:
        pwn = (project.get("path_with_namespace") or "").strip().lower()
        return pwn == scope_norm_lower or pwn.startswith(f"{scope_norm_lower}/")

    async def _resolve_scope_projects(self, client: GitLabClient) -> tuple[list[dict[str, Any]], str]:
        scope_norm = self.group_path.strip().strip("/")
        parts = [p for p in scope_norm.split("/") if p]
        scope_lower = scope_norm.lower()

        group_error: GitLabAPIError | None = None
        try:
            projects = await client.list_group_projects(self.group_path, include_subgroups=self.include_subgroups)
            unique_projects = {int(item["id"]): item for item in projects}
            project_list = sorted(unique_projects.values(), key=lambda p: str(p.get("path_with_namespace", "")))
            if project_list:
                return project_list, "group"
        except GitLabAPIError as exc:
            group_error = exc

        # List an ancestor group with include_subgroups and keep projects under original scope.
        if len(parts) > 1:
            for depth in range(len(parts) - 1, 0, -1):
                parent = "/".join(parts[:depth])
                try:
                    ancestor_projects = await client.list_group_projects(
                        parent, include_subgroups=True,
                    )
                    filtered = [p for p in ancestor_projects if self._project_under_scope(p, scope_lower)]
                    if filtered:
                        unique_projects = {int(item["id"]): item for item in filtered}
                        project_list = sorted(
                            unique_projects.values(),
                            key=lambda p: str(p.get("path_with_namespace", "")),
                        )
                        return project_list, "group_ancestor"
                except GitLabAPIError:
                    continue

        project_error: GitLabAPIError | None = None
        try:
            project = await client.get_project(self.group_path)
            if isinstance(project, dict) and project.get("id") is not None:
                return [project], "project"
        except GitLabAPIError as exc:
            project_error = exc

        # Fallback: token may lack group-level access but can see individual
        # projects.  Search by namespace prefix via /projects API.
        try:
            ns_projects = await client.search_projects_by_namespace(self.group_path)
            if ns_projects:
                unique = {int(p["id"]): p for p in ns_projects}
                return sorted(unique.values(), key=lambda p: str(p.get("path_with_namespace", ""))), "namespace_search"
        except GitLabAPIError:
            pass

        if group_error is not None:
            raise GitLabAPIError(
                message=(
                    f"Scope '{self.group_path}' not resolved as group or project. "
                    f"Group error: {group_error}. Project error: {project_error}"
                )
            )

        if project_error is not None:
            raise GitLabAPIError(
                message=(
                    f"Group '{self.group_path}' has no accessible projects and is not a project path. "
                    f"Project error: {project_error}"
                )
            )

        raise GitLabAPIError(
            message=(
                f"No accessible projects found for scope '{self.group_path}'. "
                "Check path correctness and token permissions."
            )
        )

    def _build_summary(self, project_results: list[dict[str, Any]], total_projects: int) -> dict[str, Any]:
        severity_counter: Counter[str] = Counter()
        projects_with_findings = 0
        findings_total = 0
        warnings_total = 0

        for result in project_results:
            findings = result.get("findings", [])
            warnings = result.get("warnings", [])
            warnings_total += len(warnings)
            if findings:
                projects_with_findings += 1
                findings_total += len(findings)
            for item in findings:
                severity = str(item.get("severity", "info")).lower()
                severity_counter[severity] += 1

        return {
            "projects_total": total_projects,
            "projects_scanned": len(project_results),
            "projects_with_findings": projects_with_findings,
            "findings_total": findings_total,
            "warnings_total": warnings_total,
            "severity": {
                "critical": severity_counter.get("critical", 0),
                "high": severity_counter.get("high", 0),
                "medium": severity_counter.get("medium", 0),
                "low": severity_counter.get("low", 0),
                "info": severity_counter.get("info", 0),
            },
        }

    async def _scan_project(self, client: GitLabClient, project: dict[str, Any]) -> dict[str, Any]:
        project_id = int(project["id"])
        project_path = str(project.get("path_with_namespace", project_id))
        project_url = str(project.get("web_url", ""))

        findings: list[dict[str, str]] = []
        warnings: list[str] = []

        project_details = await self._safe_call(client.get_project(project_id), warnings, "load project settings")
        if not isinstance(project_details, dict):
            project_details = project

        default_branch = str(project_details.get("default_branch") or project.get("default_branch") or "")

        protected_branches = await self._safe_call(
            client.list_protected_branches(project_id), warnings, "load protected branches", fallback=None
        )
        approvals = await self._safe_call(client.get_project_approvals(project_id), warnings, "load approvals")
        push_rule = await self._safe_call(
            client.get_project_push_rule(project_id), warnings, "load push rule", fallback={"_scanner_unavailable": True}
        )
        variables = await self._safe_call(
            client.list_project_variables(project_id), warnings, "load CI/CD variables", fallback=[]
        )

        findings.extend(
            analyze_project_settings(
                project=project_details,
                protected_branches=protected_branches if isinstance(protected_branches, list) else None,
                approvals=approvals if isinstance(approvals, dict) else None,
                push_rule=push_rule if isinstance(push_rule, dict) or push_rule is None else {"_scanner_unavailable": True},
                variables=variables if isinstance(variables, list) else [],
            )
        )

        ci_documents: list[dict[str, Any]] = []
        include_records: list[dict[str, Any]] = []
        lint_result: dict[str, Any] | None = None

        if default_branch:
            root_content = await self._safe_call(
                client.get_project_file(project_id, ".gitlab-ci.yml", default_branch), warnings, "load .gitlab-ci.yml"
            )
        else:
            root_content = None
            warnings.append("No default branch configured")

        if isinstance(root_content, str):
            parsed_root, parse_error = self._parse_yaml(root_content)
            ci_documents.append(
                {
                    "name": ".gitlab-ci.yml",
                    "project": project_path,
                    "project_id": project_id,
                    "ref": default_branch,
                    "parsed": parsed_root,
                }
            )

            if parse_error:
                findings.append(
                    finding(
                        "high",
                        "ROOT_CI_PARSE_ERROR",
                        "Failed to parse root .gitlab-ci.yml",
                        "Root pipeline file is invalid YAML.",
                        "Fix YAML syntax in .gitlab-ci.yml.",
                        parse_error,
                    )
                )

            resolved_docs, resolved_includes = await self._resolve_includes(
                client=client,
                base_project=project_id,
                base_project_path=project_path,
                base_ref=default_branch,
                root_document=parsed_root,
            )
            ci_documents.extend(resolved_docs)
            include_records.extend(resolved_includes)

            lint_result = await self._safe_call(client.lint_ci(project_id, root_content), warnings, "CI lint")
        else:
            findings.append(
                finding(
                    "medium",
                    "CI_FILE_MISSING",
                    "Root .gitlab-ci.yml was not found",
                    "No root CI config was found in the default branch.",
                    "Add .gitlab-ci.yml or verify default branch and repository layout.",
                    f"project={project_path}; branch={default_branch or 'unknown'}",
                )
            )

        findings.extend(analyze_ci_configuration(ci_documents, include_records, lint_result if isinstance(lint_result, dict) else None))
        findings = sort_findings(findings)

        return {
            "project_id": project_id,
            "project_path": project_path,
            "project_url": project_url,
            "default_branch": default_branch,
            "findings": findings,
            "findings_summary": summarize_findings(findings),
            "warnings": warnings,
            "include_records": include_records,
            "ci_documents_count": len(ci_documents),
        }

    async def _resolve_includes(
        self,
        *,
        client: GitLabClient,
        base_project: int,
        base_project_path: str,
        base_ref: str,
        root_document: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(root_document, dict):
            return [], []

        include_records: list[dict[str, Any]] = []
        ci_documents: list[dict[str, Any]] = []
        visited: set[str] = set()

        queue: list[dict[str, Any]] = []
        for include in self._normalize_includes(root_document.get("include")):
            include["depth"] = 1
            include["parent"] = ".gitlab-ci.yml"
            include["context_project"] = base_project
            include["context_project_path"] = base_project_path
            include["context_ref"] = base_ref
            queue.append(include)

        while queue:
            include = queue.pop(0)
            depth = int(include.get("depth", 1))
            include_type = str(include.get("type", "unknown"))

            record: dict[str, Any] = {
                "type": include_type,
                "depth": depth,
                "parent": include.get("parent"),
                "raw": include.get("raw"),
            }

            if depth > self.max_include_depth:
                record["status"] = "error_max_depth"
                include_records.append(record)
                continue

            document_name = ""
            next_project = include.get("context_project")
            next_project_path = include.get("context_project_path")
            next_ref = include.get("context_ref")

            if include_type == "remote":
                record["status"] = "skipped_remote"
                record["url"] = include.get("url")
                include_records.append(record)
                continue

            if include_type == "artifact":
                record["status"] = "unresolved_artifact"
                record["artifact"] = include.get("artifact")
                record["job"] = include.get("job")
                include_records.append(record)
                continue

            content: str | None = None

            try:
                if include_type == "local":
                    file_path = str(include.get("file"))
                    project_id = int(include.get("context_project"))
                    ref = str(include.get("context_ref") or "HEAD")
                    key = json.dumps({"type": include_type, "project": project_id, "file": file_path, "ref": ref})
                    if key in visited:
                        continue
                    visited.add(key)
                    content = await client.get_project_file(project_id, file_path, ref)
                    document_name = f"{include.get('context_project_path')}::{file_path}@{ref}"
                    record.update({"project": include.get("context_project_path"), "file": file_path, "ref": ref})

                elif include_type == "project":
                    include_project = str(include.get("project"))
                    file_path = str(include.get("file"))
                    include_ref = include.get("ref")
                    ref = str(include_ref or await self._resolve_default_branch(client, include_project))
                    key = json.dumps({"type": include_type, "project": include_project, "file": file_path, "ref": ref})
                    if key in visited:
                        continue
                    visited.add(key)
                    content = await client.get_project_file(include_project, file_path, ref)
                    document_name = f"{include_project}::{file_path}@{ref}"
                    next_project = include_project
                    next_project_path = include_project
                    next_ref = ref
                    record.update({"project": include_project, "file": file_path, "ref": include_ref})

                elif include_type == "template":
                    template_name = str(include.get("template"))
                    key = json.dumps({"type": include_type, "template": template_name})
                    if key in visited:
                        continue
                    visited.add(key)
                    content = await client.get_ci_template(template_name)
                    document_name = f"template::{template_name}"
                    record.update({"template": template_name})

                else:
                    record["status"] = "ignored_unknown_include"
                    include_records.append(record)
                    continue

                parsed, parse_error = self._parse_yaml(content)
                ci_documents.append(
                    {
                        "name": document_name,
                        "project": next_project_path,
                        "project_id": next_project,
                        "ref": next_ref,
                        "parsed": parsed,
                    }
                )

                if parse_error:
                    record["status"] = "error_parse"
                    record["error"] = parse_error
                    include_records.append(record)
                    continue

                record["status"] = "resolved"
                include_records.append(record)

                if isinstance(parsed, dict):
                    for nested in self._normalize_includes(parsed.get("include")):
                        nested["depth"] = depth + 1
                        nested["parent"] = document_name
                        nested["context_project"] = next_project
                        nested["context_project_path"] = next_project_path
                        nested["context_ref"] = next_ref
                        queue.append(nested)

            except Exception as exc:  # noqa: BLE001
                record["status"] = "error_fetch"
                record["error"] = str(exc)
                include_records.append(record)

        return ci_documents, include_records

    async def _resolve_default_branch(self, client: GitLabClient, project: str) -> str:
        cached = self._project_default_branch_cache.get(project)
        if cached:
            return cached
        details = await client.get_project(project)
        default_branch = str(details.get("default_branch") or "HEAD")
        self._project_default_branch_cache[project] = default_branch
        return default_branch

    async def _safe_call(
        self,
        awaitable: Awaitable[Any],
        warnings: list[str],
        action: str,
        fallback: Any = None,
    ) -> Any:
        try:
            return await awaitable
        except GitLabAPIError as exc:
            warnings.append(f"{action}: {exc}")
            return fallback
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{action}: {exc}")
            return fallback

    def _normalize_includes(self, include_value: Any) -> list[dict[str, Any]]:
        if include_value is None:
            return []
        raw_items: list[Any] = include_value if isinstance(include_value, list) else [include_value]
        normalized: list[dict[str, Any]] = []

        for raw in raw_items:
            if isinstance(raw, str):
                normalized.append({"type": "local", "file": raw, "raw": raw})
                continue
            if not isinstance(raw, dict):
                continue
            if "local" in raw:
                for fp in self._coerce_to_list(raw.get("local")):
                    normalized.append({"type": "local", "file": str(fp), "raw": raw})
                continue
            if "project" in raw and "file" in raw:
                proj = str(raw.get("project"))
                ref = raw.get("ref")
                for fp in self._coerce_to_list(raw.get("file")):
                    normalized.append({"type": "project", "project": proj, "file": str(fp), "ref": ref, "raw": raw})
                continue
            if "template" in raw:
                for t in self._coerce_to_list(raw.get("template")):
                    normalized.append({"type": "template", "template": str(t), "raw": raw})
                continue
            if "remote" in raw:
                normalized.append({"type": "remote", "url": str(raw.get("remote")), "raw": raw})
                continue
            if "artifact" in raw:
                normalized.append({
                    "type": "artifact",
                    "artifact": str(raw.get("artifact")),
                    "job": str(raw.get("job") or ""),
                    "project": str(raw.get("project") or ""),
                    "ref": str(raw.get("ref") or ""),
                    "raw": raw,
                })
                continue

        return normalized

    @staticmethod
    def _coerce_to_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _parse_yaml(content: str) -> tuple[Any, str | None]:
        try:
            parsed = yaml.safe_load(content)
            return parsed if parsed is not None else {}, None
        except yaml.YAMLError as exc:
            return None, str(exc)
