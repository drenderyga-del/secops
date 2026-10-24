from __future__ import annotations

import os
import json
import re
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from .integration_settings import runtime_setting

logger = logging.getLogger(__name__)


@dataclass
class AsocUploadResult:
    status: str
    product_id: str | None = None
    product_slug: str | None = None
    message: str | None = None
    matched_by: str | None = None


def _configured_host_prefixes() -> dict[str, str]:
    try:
        values = json.loads(os.getenv("ASOC_GITLAB_HOST_PREFIXES", "") or "{}")
    except ValueError:
        raise ValueError("ASOC_GITLAB_HOST_PREFIXES must be a JSON object") from None
    if not isinstance(values, dict):
        raise ValueError("ASOC_GITLAB_HOST_PREFIXES must be a JSON object")
    result = {}
    for host, prefix in values.items():
        if not isinstance(host, str) or not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,100}", prefix):
            raise ValueError("ASOC host prefixes must be safe slug segments")
        result[host.casefold().rstrip(".")] = prefix
    return result


def host_slug_prefix(gitlab_url: str) -> str:
    parsed = urlparse(gitlab_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    configured = _configured_host_prefixes()
    if host in configured:
        return configured[host]
    # Include the complete host so separate GitLab instances do not collapse
    # into the same first-label namespace.
    authority = host + (f"-{parsed.port}" if parsed.port and parsed.port != 443 else "")
    return re.sub(r"[^a-z0-9_-]+", "-", authority).strip("-") or "gitlab"


def product_slug_for_path(gitlab_url: str, project_path: str, scope_path: str | None = None) -> str:
    prefix = host_slug_prefix(gitlab_url)
    scope = (scope_path or "").strip().strip("/")
    if scope:
        product_path = scope
    else:
        parts = [p for p in (project_path or "").strip("/").split("/") if p]
        product_path = "/".join(parts[:-1]) if len(parts) > 1 else (parts[0] if parts else "unknown")
    return f"{prefix}/{product_path.strip('/')}"


def repository_slug_for_path(gitlab_url: str, project_path: str) -> str:
    return f"{host_slug_prefix(gitlab_url)}/{project_path.strip('/')}"


# ── ASOC repository identity (ported from asoc/asoc_repository_audit.py) ──
#
# ASOC slugs are unreliable: legacy repositories live under ci/<name> while
# canonical ones are <instance-prefix>/<project_path>. The stable identity of an ASOC
# repository is the GitLab URL it points to — its own ``url`` field and/or
# the ``externalLink.url`` of the VCS binding.


def append_error(existing: str | None, message: str | None) -> str | None:
    if not message:
        return existing
    if not existing:
        return message
    # Re-uploads run the same resolution again — don't stack identical notes.
    if message in existing:
        return existing
    return f"{existing}; {message}"


def normalize_repo_url(value: str | None) -> str:
    """Comparison form of a repository URL.

    ASOC descriptions sometimes contain links copied from a browser, so query
    strings and fragments are presentation details rather than repository
    identity. Keep the URL path exact while normalizing those details away.
    """
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.netloc.endswith(":")
    ):
        return ""
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path.lower(), "", ""))


def repository_identity_urls(repo: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    external_link = repo.get("externalLink")
    external_url = external_link.get("url") if isinstance(external_link, dict) else None
    for value in (repo.get("url"), external_url):
        normalized = normalize_repo_url(value)
        if normalized and normalized not in urls:
            urls.append(normalized)
    return urls


def _is_vcs_linked(repo: dict[str, Any]) -> bool:
    external_link = repo.get("externalLink")
    return isinstance(external_link, dict) and bool(external_link.get("url"))


def _is_canonical_slug(slug: str | None) -> bool:
    prefix = (slug or "").split("/", 1)[0].casefold()
    return bool(prefix) and prefix in set(_configured_host_prefixes().values())


def _parse_asoc_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _last_activity_ts(repo: dict[str, Any]) -> float:
    pipeline = repo.get("lastScanPipeline")
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    orchestration = repo.get("lastOrchestrationScan")
    orchestration = orchestration if isinstance(orchestration, dict) else {}
    candidates = [
        repo.get("lastScan"),
        pipeline.get("finished") or pipeline.get("updated") or pipeline.get("created"),
        orchestration.get("updated") or orchestration.get("created"),
        repo.get("lastAggregated"),
        repo.get("created"),
    ]
    parsed = [dt for dt in (_parse_asoc_datetime(v) for v in candidates if v) if dt is not None]
    return max(dt.timestamp() for dt in parsed) if parsed else 0.0


def _repository_preference(repo: dict[str, Any]) -> tuple:
    """Sort key for several ASOC repositories pointing at one GitLab repo:
    VCS-linked beats unlinked, canonical slug beats legacy ci/*, then the
    most recently active card wins."""
    return (
        _is_vcs_linked(repo),
        _is_canonical_slug(repo.get("slug")) or any(
            (repo.get("slug") or "").startswith(host_slug_prefix(url) + "/")
            for url in repository_identity_urls(repo)
        ),
        _last_activity_ts(repo),
    )


class AsocClient:
    def __init__(self) -> None:
        self.url = runtime_setting("HEXWAY_URL", "https://asoc.example.com/api").rstrip("/")
        self.token = runtime_setting("HEXWAY_TOKEN", "")
        self.timeout = int(os.getenv("HEXWAY_TIMEOUT", "30"))
        # URL matching may need per-card detail requests when the list payload
        # omits url/externalLink; cap them so one upload can't spam the API.
        self.max_detail_lookups = int(os.getenv("ASOC_MAX_DETAIL_LOOKUPS", "20"))
        self.verify_ssl = os.getenv("ASOC_VERIFY_SSL", "true").lower() in {"1", "true", "yes"}
        if not self.verify_ssl:
            urllib3.disable_warnings(InsecureRequestWarning)

    def enabled(self) -> bool:
        return bool(self.url and self.token)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def find_repository(self, slug: str, project_url: str | None = None) -> dict[str, Any] | None:
        """Existing ASOC repository card for a GitLab project.

        Lookup order:
        1. exact slug match (<instance-prefix>/<project_path>) — the canonical naming;
        2. by GitLab URL: an ASOC repository whose ``url`` or
           ``externalLink.url`` (VCS binding) points at ``project_url`` — this
           finds repositories that already exist under a different slug
           (legacy ci/* cards, renamed projects).

        Repositories are created by hexway prepare during ptai_scan (or by
        hand); the IaC upload NEVER creates products or repositories — it only
        pushes results into a repository that already exists. The returned
        dict carries ``_matched_by`` ("slug" | "url") for logging/UI.
        """
        if not self.enabled():
            return None
        found = self._find_repository_by_slug(slug)
        if found:
            found["_matched_by"] = "slug"
            return found
        if project_url:
            found = self._find_repository_by_url(project_url)
            if found:
                found["_matched_by"] = "url"
                return found
        return None

    def upload_sarif(
        self,
        *,
        sarif_path: str,
        repository_slug: str,
        project_url: str,
        branch: str | None,
    ) -> AsocUploadResult:
        if not self.enabled():
            return AsocUploadResult(status="skipped", message="ASOC is not connected in Integrations")
        file_path = Path(sarif_path)
        if not file_path.is_file():
            return AsocUploadResult(status="failed", message=f"SARIF file missing: {sarif_path}")
        data = {
            "scannerType": "SARIF",
            "repository": repository_slug,
            "repositoryBranch": branch or "main",
            "vcsURL": project_url,
        }
        try:
            with file_path.open("rb") as fh:
                response = requests.post(
                    f"{self.url}/scan_uploads/",
                    headers=self.headers,
                    data=data,
                    files={"scannerResultFile": (file_path.name, fh, "application/json")},
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
        except requests.RequestException as exc:
            return AsocUploadResult(status="failed", message=str(exc))
        if response.status_code == 200:
            return AsocUploadResult(status="uploaded")
        return AsocUploadResult(
            status="failed",
            message=f"HTTP {response.status_code}: {response.text[:300]}",
        )

    def get_scan_pipeline_status(self, pipeline_id: str) -> dict[str, Any] | None:
        """Minimal scan-pipeline status for polling (ext/v1 external API).

        Returns the parsed JSON ({id, status, created, finished, ...}) or None
        on transport error / non-200. A 404 (pipeline unknown) returns None too;
        the caller decides how to treat a missing pipeline.
        """
        path = f"/ext/v1/scan/pipelines/{pipeline_id}/status/"
        try:
            response = requests.get(
                f"{self.url}{path}",
                headers=self.headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            logger.warning(
                "asoc_status_http_error source=pipeline pipeline_id=%s path=%s verify_ssl=%s error=%s",
                pipeline_id,
                path,
                self.verify_ssl,
                exc,
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "asoc_status_http_response source=pipeline pipeline_id=%s path=%s http_code=%s content_type=%s body=%s",
                pipeline_id,
                path,
                response.status_code,
                response.headers.get("content-type"),
                (response.text or "")[:500],
            )
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning(
                "asoc_status_parse_error source=pipeline pipeline_id=%s path=%s http_code=%s content_type=%s body=%s",
                pipeline_id,
                path,
                response.status_code,
                response.headers.get("content-type"),
                (response.text or "")[:500],
            )
            return None
        logger.info(
            "asoc_status_result source=pipeline pipeline_id=%s raw_status=%s finished=%s created=%s keys=%s",
            pipeline_id,
            body.get("status"),
            body.get("finished"),
            body.get("created"),
            sorted(body.keys()) if isinstance(body, dict) else None,
        )
        return body

    def get_background_task(self, task_id: str) -> dict[str, Any] | None:
        """Background-task details (ext/v1) — used for errorDetails on failure."""
        path = f"/ext/v1/background_tasks/{task_id}/"
        try:
            response = requests.get(
                f"{self.url}{path}",
                headers=self.headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            logger.warning(
                "asoc_status_http_error source=task task_id=%s path=%s verify_ssl=%s error=%s",
                task_id,
                path,
                self.verify_ssl,
                exc,
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "asoc_status_http_response source=task task_id=%s path=%s http_code=%s content_type=%s body=%s",
                task_id,
                path,
                response.status_code,
                response.headers.get("content-type"),
                (response.text or "")[:500],
            )
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning(
                "asoc_status_parse_error source=task task_id=%s path=%s http_code=%s content_type=%s body=%s",
                task_id,
                path,
                response.status_code,
                response.headers.get("content-type"),
                (response.text or "")[:500],
            )
            return None
        logger.info(
            "asoc_status_result source=task task_id=%s raw_status=%s updated=%s created=%s keys=%s",
            task_id,
            body.get("status"),
            body.get("updated"),
            body.get("created"),
            sorted(body.keys()) if isinstance(body, dict) else None,
        )
        return body

    def list_products(self, *, page_size: int = 100, max_items: int = 10000) -> list[dict[str, Any]]:
        """All product cards from `GET /ext/v1/products/` (paginated).

        Raises RuntimeError on the first failed or truncated page so a partial
        sync is never mistaken for a full one.
        """
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                response = requests.get(
                    f"{self.url}/ext/v1/products/",
                    headers=self.headers,
                    params={"limit": page_size, "offset": offset},
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            except requests.RequestException as exc:
                raise RuntimeError("ASOC products request failed") from exc
            if response.status_code != 200:
                raise RuntimeError(f"ASOC products returned HTTP {response.status_code}")
            try:
                body = response.json()
            except (TypeError, ValueError) as exc:
                raise RuntimeError("ASOC products returned invalid JSON") from exc
            if not isinstance(body, dict):
                raise RuntimeError("ASOC products response has an invalid schema")
            page = body.get("items")
            has_next = body.get("hasNext")
            if not isinstance(page, list) or not isinstance(has_next, bool):
                raise RuntimeError("ASOC products response has an invalid schema")
            if any(
                not isinstance(item, dict)
                or isinstance(item.get("id"), bool)
                or not isinstance(item.get("id"), (str, int))
                or not str(item.get("id") or "").strip()
                for item in page
            ):
                raise RuntimeError("ASOC products response has an invalid product entry")
            if len(items) + len(page) > max_items:
                raise RuntimeError(
                    "ASOC products response exceeded the safety limit "
                    f"of {max_items} items; refusing an incomplete snapshot"
                )
            items.extend(page)
            if not has_next:
                break
            if not page:
                raise RuntimeError(
                    "ASOC products returned an empty page while hasNext is true; "
                    "refusing an incomplete snapshot"
                )
            if len(items) >= max_items:
                raise RuntimeError(
                    "ASOC products reached the safety limit "
                    f"of {max_items} items while more pages remain; "
                    "refusing an incomplete snapshot"
                )
            offset += len(page)
        return items

    def list_product_repositories(
        self,
        product_id: str,
        *,
        page_size: int = 100,
        max_items: int = 10000,
    ) -> list[dict[str, Any]]:
        """All ASOC repositories linked to one product.

        Product membership is read from the supported external API filter. The
        list schema includes ``url``; when a real installation omits every
        identity URL, fetch that repository's detail card for
        ``externalLink.url`` before returning it to the mapper.
        """
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if max_items <= 0:
            return []
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                response = requests.get(
                    f"{self.url}/ext/v1/repositories/",
                    headers=self.headers,
                    params={
                        "product": product_id,
                        "limit": page_size,
                        "offset": offset,
                    },
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"ASOC repositories request failed for product {product_id}: {exc}"
                ) from exc
            if response.status_code != 200:
                raise RuntimeError(
                    "ASOC repositories returned HTTP "
                    f"{response.status_code} for product {product_id}: "
                    f"{(response.text or '')[:300]}"
                )
            body = response.json() or {}
            page = body.get("items") or []
            has_next = bool(body.get("hasNext"))
            if len(items) + len(page) > max_items:
                raise RuntimeError(
                    "ASOC repositories response exceeded the safety limit "
                    f"of {max_items} items; refusing an incomplete snapshot"
                )
            items.extend(page)
            if not has_next:
                break
            if not page:
                raise RuntimeError(
                    "ASOC repositories returned an empty page while hasNext is true; "
                    "refusing an incomplete snapshot"
                )
            if len(items) >= max_items:
                raise RuntimeError(
                    "ASOC repositories reached the safety limit "
                    f"of {max_items} items while more pages remain; "
                    "refusing an incomplete snapshot"
                )
            offset += len(page)

        resolved: list[dict[str, Any]] = []
        for item in items:
            if repository_identity_urls(item):
                resolved.append(item)
                continue
            details = self._get_repository(item.get("id"))
            resolved.append({**item, **details} if details else item)
        return resolved

    def debug_get(self, path: str) -> dict[str, Any]:
        """Raw GET for the PT AI scan debug panel: returns HTTP code + body.

        Unlike the polling helpers above (which collapse every non-200 into
        ``None``), this surfaces the actual status code and payload so an
        operator can see *why* a scan looks stuck (404, 403, unexpected body…).
        Never raises.
        """
        url = path if path.startswith("http") else f"{self.url}{path}"
        entry: dict[str, Any] = {"url": url}
        try:
            response = requests.get(
                url, headers=self.headers, timeout=self.timeout, verify=self.verify_ssl
            )
            entry["code"] = response.status_code
            try:
                entry["body"] = response.json()
            except ValueError:
                entry["body"] = (response.text or "")[:2000]
        except requests.RequestException as exc:
            entry["error"] = str(exc)
        return entry

    def _find_repository_by_slug(self, slug: str) -> dict[str, Any] | None:
        for item in self._search_repositories(slug):
            if str(item.get("slug") or "").lower() == slug.lower():
                return item
        return None

    def _find_repository_by_url(self, project_url: str) -> dict[str, Any] | None:
        target = normalize_repo_url(project_url)
        if not target:
            return None
        # ASOC search matches name/slug, not URL, so search by the project
        # name (last path segment) and compare identity URLs client-side.
        project_name = target.rsplit("/", 1)[-1]
        items = self._search_repositories(project_name)
        matches: list[dict[str, Any]] = []
        needs_details: list[dict[str, Any]] = []
        for item in items:
            urls = repository_identity_urls(item)
            if target in urls:
                matches.append(item)
            elif not urls:
                # The list payload may omit url/externalLink — decide after
                # fetching the card details.
                needs_details.append(item)
        for item in needs_details[: self.max_detail_lookups]:
            details = self._get_repository(item.get("id"))
            if not details:
                continue
            merged = {**item, **details}
            if target in repository_identity_urls(merged):
                matches.append(merged)
        if not matches:
            return None
        return max(matches, key=_repository_preference)

    def _search_repositories(self, query: str) -> list[dict[str, Any]]:
        try:
            response = requests.get(
                f"{self.url}/repositories/",
                headers=self.headers,
                params={"search": query, "limit": 100},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException:
            return []
        if response.status_code != 200:
            return []
        return (response.json() or {}).get("items", []) or []

    def _get_repository(self, repo_id: Any) -> dict[str, Any] | None:
        if not repo_id:
            return None
        try:
            response = requests.get(
                f"{self.url}/repositories/{repo_id}/",
                headers=self.headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def create_repository(
        self,
        *,
        slug: str,
        project_path: str,
        project_url: str,
        product_id: str | None,
    ) -> dict[str, Any] | None:
        """Create an ASOC repository for a manual IaC scan.

        The ASOC product is optional: a repository with its GitLab URL is a
        valid target even when the dedicated IaC product is not configured.
        """
        payload: dict[str, Any] = {
            "name": project_path.rsplit("/", 1)[-1] or project_path,
            "slug": slug,
            "url": project_url,
            "description": "Created by ScanOps IaC scan",
            "criticality": "MEDIUM",
            "type": "BINARY",
        }
        if product_id:
            payload["productID"] = product_id
        try:
            response = requests.post(
                f"{self.url}/repositories/",
                headers=self.headers,
                json=payload,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            logger.warning("asoc_repository_create_error slug=%s error=%s", slug, exc)
            return None
        if response.status_code in {200, 201}:
            try:
                return response.json()
            except ValueError:
                return None
        # A concurrent manual scan may have won the create race. Resolve it
        # again rather than reporting a false failure.
        if response.status_code == 400:
            return self.find_repository(slug, project_url=project_url)
        logger.warning(
            "asoc_repository_create_failed slug=%s http_code=%s body=%s",
            slug,
            response.status_code,
            (response.text or "")[:500],
        )
        return None

    def upload_repo_row(self, row: Any, gitlab_base_url: str) -> AsocUploadResult:
        """Resolve the ASOC repository for an IacScanRepository row and push
        its SARIF. Shared by the IaC worker (post-scan) and the web re-upload
        action (SARIF already on disk, no rescan).

        Mutates ``row.asoc_*`` fields and records actual upload failures in
        ``row.error_text``. A missing repository is created with the GitLab URL. New repositories
        use IAC_HEXWAY_PRODUCT_ID when configured. The caller commits. This
        never creates ASOC products.
        """
        computed_slug = repository_slug_for_path(gitlab_base_url, row.project_path)
        row.asoc_repository_slug = computed_slug
        if not self.enabled():
            message = "ASOC is not connected in Integrations"
            row.asoc_upload_status = "skipped"
            row.error_text = append_error(row.error_text, message)
            return AsocUploadResult(status="skipped", message=message)
        asoc_repo = self.find_repository(computed_slug, project_url=row.project_url)
        if not asoc_repo:
            product_id = (os.getenv("IAC_HEXWAY_PRODUCT_ID") or "").strip() or None
            asoc_repo = self.create_repository(
                slug=computed_slug,
                project_path=row.project_path,
                project_url=row.project_url or "",
                product_id=product_id,
            )
            if not asoc_repo:
                message = (
                    f"ASOC repository could not be created for slug '{computed_slug}' "
                    f"and URL '{row.project_url}'"
                )
                row.asoc_upload_status = "failed"
                row.error_text = append_error(row.error_text, message)
                return AsocUploadResult(status="failed", message=message)
            row.asoc_product_id = product_id
            logger.info("asoc_repository_created slug=%s repository_id=%s", computed_slug, asoc_repo.get("id"))
            asoc_repo["_matched_by"] = "created"
        # Upload into the slug the ASOC card actually has — it may differ from
        # the computed <instance-prefix>/<path> (legacy ci/* cards matched by URL).
        repository_slug = str(asoc_repo.get("slug") or "") or computed_slug
        matched_by = asoc_repo.get("_matched_by")
        row.asoc_repository_slug = repository_slug
        row.asoc_repository_id = str(asoc_repo.get("id") or "") or None
        if matched_by == "url":
            logger.info("asoc_repository_matched_by_url slug=%s repository_id=%s", repository_slug, row.asoc_repository_id)
        upload = self.upload_sarif(
            sarif_path=row.sarif_path or "",
            repository_slug=repository_slug,
            project_url=row.project_url or "",
            branch=row.default_branch,
        )
        upload.matched_by = matched_by
        row.asoc_upload_status = upload.status
        if upload.message:
            row.error_text = append_error(row.error_text, upload.message)
        return upload
