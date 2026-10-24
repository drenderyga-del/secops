"""Minimal PT AI (Application Inspector) API client for scan status.

ASOC orchestrates the scan but does not expose the PT AI scan id, so we resolve
the engine-side status by project name. ASOC names each PT AI project
``vampy-asoc-<repository slug>`` (configurable via ``PTAI_PROJECT_PREFIX``), then:

    project name → projectId → branch (by name) → branchId
    → branches/{branchId}/scanResults/last → stage / progress / errors

Auth: a long-lived access token is exchanged for a JWT via GET /api/auth/signin
(header ``Access-Token``); the JWT is then sent as ``Authorization: Bearer``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from .integration_settings import runtime_setting

logger = logging.getLogger(__name__)


def ptai_result_url(scan: Any, base_url: Optional[str] = None, template: Optional[str] = None) -> Optional[str]:
    """Direct PT AI result URL for a persisted engine-side scan.

    PT AI 5.x exposes a scan result under the branch resource. Deployments
    whose web UI uses a different deep-link route can override only the path
    through ``PTAI_RESULT_URL_TEMPLATE``; the host still comes from PTAI_URL.

    The default is the PT AI web interface route, not an API endpoint.
    """
    project_id = getattr(scan, "ptai_project_id", None)
    branch_id = getattr(scan, "ptai_branch_id", None)
    scan_result_id = getattr(scan, "ptai_scan_result_id", None)
    if not branch_id or not scan_result_id:
        return None
    base_url = (base_url if base_url is not None else runtime_setting("PTAI_URL", "")).strip().rstrip("/")
    # PTAI_URL is the API origin used by the client (often ending in /api),
    # while result links must point at the web application origin.  Keep the
    # API setting intact for requests and only normalize this presentation URL.
    if base_url.lower().endswith("/api"):
        base_url = base_url[:-4].rstrip("/")
    if not base_url or not branch_id or not scan_result_id:
        return None

    values = {
        "base_url": base_url,
        "project_id": quote(str(project_id or ""), safe=""),
        "branch_id": quote(str(branch_id), safe=""),
        "scan_result_id": quote(str(scan_result_id), safe=""),
    }
    template = (template if template is not None else runtime_setting("PTAI_RESULT_URL_TEMPLATE", "")).strip()
    if not project_id and not template:
        return None
    if template:
        try:
            return template.format_map(values)
        except (KeyError, ValueError):
            logger.warning("Invalid PTAI_RESULT_URL_TEMPLATE; falling back to the PT AI web result URL")
    return (
        f"{base_url}/ui/projects/{values['project_id']}"
        f"/branches/{values['branch_id']}/scans/{values['scan_result_id']}"
    )


@dataclass
class PtaiEngineStatus:
    project_id: str
    branch_id: str
    scan_result_id: Optional[str]
    stage: Optional[str]
    progress: Optional[int]
    has_errors: bool
    error: Optional[str] = None


class PtaiClient:
    def __init__(self) -> None:
        self.url = runtime_setting("PTAI_URL", "").rstrip("/")
        self.token = runtime_setting("PTAI_TOKEN", "")
        self.project_prefix = os.getenv("PTAI_PROJECT_PREFIX", "vampy-asoc-")
        self.timeout = int(os.getenv("PTAI_TIMEOUT", "30"))
        self.verify_ssl = runtime_setting("PTAI_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"}
        self._jwt: Optional[str] = None
        if not self.verify_ssl:
            urllib3.disable_warnings(InsecureRequestWarning)

    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def project_name_for(self, repo_slug: str) -> str:
        return f"{self.project_prefix}{repo_slug}"

    # ── auth ─────────────────────────────────────────────────────────

    def _sign_in(self) -> bool:
        logger.info("ptai_signin_start url=%s verify_ssl=%s timeout=%s", self.url, self.verify_ssl, self.timeout)
        try:
            response = requests.get(
                f"{self.url}/api/auth/signin",
                headers={"Access-Token": self.token},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            logger.warning("ptai_signin_transport_error url=%s verify_ssl=%s error=%s", self.url, self.verify_ssl, exc)
            return False
        if response.status_code != 200:
            logger.warning(
                "ptai_signin_http_error url=%s http_code=%s body=%s",
                self.url,
                response.status_code,
                response.text[:500],
            )
            return False
        self._jwt = (response.json() or {}).get("accessToken")
        logger.info("ptai_signin_finish url=%s has_token=%s", self.url, bool(self._jwt))
        return bool(self._jwt)

    def _get(self, path: str) -> Optional[requests.Response]:
        """GET with the cached JWT, re-signing once on 401/empty token."""
        if not self._jwt and not self._sign_in():
            return None
        url = f"{self.url}{path}"
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {self._jwt}"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            if response.status_code == 401 and self._sign_in():
                logger.info("ptai_get_retry_after_401 path=%s", path)
                response = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self._jwt}"},
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
        except requests.RequestException as exc:
            logger.warning("ptai_get_transport_error path=%s verify_ssl=%s error=%s", path, self.verify_ssl, exc)
            return None
        if response.status_code >= 400:
            logger.warning(
                "ptai_get_http_error path=%s http_code=%s body=%s",
                path,
                response.status_code,
                response.text[:500],
            )
        return response

    # ── lookups ──────────────────────────────────────────────────────

    def get_project_id(self, name: str) -> Optional[str]:
        resp = self._get(f"/api/projects/name/{quote(name, safe='')}")
        if resp is None or resp.status_code != 200:
            logger.info("ptai_project_resolve_miss project=%s http_code=%s", name, resp.status_code if resp else None)
            return None
        return (resp.json() or {}).get("id")

    @staticmethod
    def _json_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("items", "results", "data", "projects"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def list_projects(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Best-effort project listing for queue monitoring."""
        query = urlencode({"limit": limit})
        resp = self._get(f"/api/projects?{query}")
        if resp is None or resp.status_code != 200:
            logger.info("ptai_projects_list_failed http_code=%s", resp.status_code if resp else None)
            return []
        items = self._json_items(resp.json())
        logger.info("ptai_projects_list_loaded count=%s limit=%s", len(items), limit)
        return items

    def list_branches(self, project_id: str) -> list[dict[str, Any]]:
        resp = self._get(f"/api/projects/{project_id}/branches")
        if resp is None or resp.status_code != 200:
            logger.info("ptai_branches_list_failed project_id=%s http_code=%s", project_id, resp.status_code if resp else None)
            return []
        items = self._json_items(resp.json())
        logger.debug("ptai_branches_list_loaded project_id=%s count=%s", project_id, len(items))
        return items

    def get_branch_id(self, project_id: str, branch_name: str) -> Optional[str]:
        target = (branch_name or "").strip().lower()
        for branch in self.list_branches(project_id):
            if str(branch.get("name") or "").strip().lower() == target:
                return branch.get("id")
        logger.info("ptai_branch_resolve_miss project_id=%s branch=%s", project_id, branch_name)
        return None

    def get_last_scan_result(self, branch_id: str) -> Optional[dict[str, Any]]:
        resp = self._get(f"/api/branches/{branch_id}/scanResults/last")
        if resp is None or resp.status_code != 200:
            logger.info("ptai_last_scan_result_failed branch_id=%s http_code=%s", branch_id, resp.status_code if resp else None)
            return None
        return resp.json()

    def get_scan_errors(self, project_id: str, scan_result_id: str) -> Optional[str]:
        resp = self._get(f"/api/projects/{project_id}/scanResults/{scan_result_id}/errors")
        if resp is None or resp.status_code != 200:
            logger.info(
                "ptai_scan_errors_failed project_id=%s scan_result_id=%s http_code=%s",
                project_id,
                scan_result_id,
                resp.status_code if resp else None,
            )
            return None
        errors = resp.json() or []
        if not isinstance(errors, list) or not errors:
            return None
        # Errors are objects; surface the first message-like field, joined.
        messages = []
        for err in errors[:5]:
            if isinstance(err, dict):
                messages.append(str(err.get("message") or err.get("title") or err))
            else:
                messages.append(str(err))
        return "; ".join(m for m in messages if m) or None

    # ── high-level resolve ────────────────────────────────────────────

    def resolve_status(
        self,
        *,
        repo_slug: str,
        branch: Optional[str],
        project_id: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> Optional[PtaiEngineStatus]:
        """Resolve engine status for a repo+branch.

        ``project_id`` / ``branch_id`` can be passed to skip the name/branch
        lookups once they have been cached on the row.
        """
        if not project_id:
            project_id = self.get_project_id(self.project_name_for(repo_slug))
            if not project_id:
                logger.info("ptai_resolve_status_miss repo_slug=%s branch=%s stage=project_lookup", repo_slug, branch)
                return None
        if not branch_id:
            branch_id = self.get_branch_id(project_id, branch or "")
            if not branch_id:
                logger.info(
                    "ptai_resolve_status_miss repo_slug=%s project_id=%s branch=%s stage=branch_lookup",
                    repo_slug,
                    project_id,
                    branch,
                )
                return None

        result = self.get_last_scan_result(branch_id)
        if result is None:
            logger.info(
                "ptai_resolve_status_no_result repo_slug=%s project_id=%s branch_id=%s branch=%s",
                repo_slug,
                project_id,
                branch_id,
                branch,
            )
            return PtaiEngineStatus(
                project_id=project_id, branch_id=branch_id, scan_result_id=None,
                stage=None, progress=None, has_errors=False,
            )
        progress = result.get("progress") or {}
        has_errors = bool(result.get("hasErrors"))
        scan_result_id = result.get("id")
        error = None
        if has_errors and scan_result_id:
            error = self.get_scan_errors(project_id, scan_result_id)
        logger.info(
            "ptai_resolve_status_finish repo_slug=%s project_id=%s branch_id=%s branch=%s scan_result_id=%s stage=%s progress=%s has_errors=%s",
            repo_slug,
            project_id,
            branch_id,
            branch,
            scan_result_id,
            progress.get("stage"),
            progress.get("value"),
            has_errors,
        )
        return PtaiEngineStatus(
            project_id=project_id,
            branch_id=branch_id,
            scan_result_id=scan_result_id,
            stage=progress.get("stage"),
            progress=progress.get("value"),
            has_errors=has_errors,
            error=error,
        )
