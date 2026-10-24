from __future__ import annotations

import asyncio
import logging
import os
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..gitlab_url import gitlab_web_origin, normalize_gitlab_origin

logger = logging.getLogger(__name__)


@dataclass
class GitLabAPIError(Exception):
    message: str
    status_code: int | None = None
    path: str | None = None

    def __str__(self) -> str:
        status = f" [{self.status_code}]" if self.status_code is not None else ""
        location = f" at {self.path}" if self.path else ""
        return f"{self.message}{status}{location}"


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
        request_timeout: int = 30,
    ) -> None:
        origin = gitlab_web_origin(base_url)
        self.base_url = origin if origin else normalize_gitlab_origin(base_url)
        headers: dict[str, str] = {}
        if token.strip():
            headers["PRIVATE-TOKEN"] = token.strip()
        headers["User-Agent"] = "secpipe-config-scanner/1.0"

        verify: bool | ssl.SSLContext = verify_ssl
        if verify_ssl:
            ca_bundle = (
                os.getenv("SSL_CERT_FILE")
                or os.getenv("REQUESTS_CA_BUNDLE")
                or os.getenv("REQUESTS_CA_BANDLE")
            )
            if ca_bundle and os.path.isfile(ca_bundle):
                ctx = ssl.create_default_context(cafile=ca_bundle)
                verify = ctx

        self.client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v4",
            headers=headers,
            timeout=request_timeout,
            verify=verify,
        )
        logger.info(
            "gitlab_client_init base_url=%s verify_ssl=%s timeout=%s has_token=%s",
            self.base_url,
            verify_ssl,
            request_timeout,
            bool(token.strip()),
        )

    async def __aenter__(self) -> "GitLabClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        attempts = 4
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.request(method, path, params=params, json=json)
            except httpx.HTTPError as exc:
                if attempt == attempts:
                    logger.warning(
                        "gitlab_api_transport_error method=%s path=%s attempt=%s params=%s error=%s",
                        method,
                        path,
                        attempt,
                        params or {},
                        exc,
                    )
                    raise GitLabAPIError(f"HTTP transport error: {exc}", path=path) from exc
                logger.info(
                    "gitlab_api_retry method=%s path=%s attempt=%s reason=transport_error error=%s",
                    method,
                    path,
                    attempt,
                    exc,
                )
                await asyncio.sleep(min(attempt, 3))
                continue

            if response.status_code in expected:
                return response

            if response.status_code in {429, 502, 503, 504} and attempt < attempts:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else float(attempt)
                logger.info(
                    "gitlab_api_retry method=%s path=%s attempt=%s http_code=%s delay=%s",
                    method,
                    path,
                    attempt,
                    response.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            detail = response.text[:500]
            logger.warning(
                "gitlab_api_http_error method=%s path=%s http_code=%s params=%s body=%s",
                method,
                path,
                response.status_code,
                params or {},
                detail,
            )
            raise GitLabAPIError(
                message=f"GitLab API request failed: {detail}",
                status_code=response.status_code,
                path=path,
            )

        raise GitLabAPIError("Unexpected request failure", path=path)

    async def paginated_get(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = []
        page = 1
        while True:
            query = dict(params or {})
            query["page"] = page
            query.setdefault("per_page", 100)
            response = await self._request("GET", path, params=query)
            chunk = response.json()
            if not isinstance(chunk, list):
                raise GitLabAPIError("Expected list response", path=path)
            data.extend(chunk)
            next_page = response.headers.get("X-Next-Page")
            if not next_page:
                break
            page = int(next_page)
        return data

    @staticmethod
    def _encode_project(project: int | str) -> str:
        if isinstance(project, int):
            return str(project)
        return quote(project, safe="")

    async def list_group_projects(self, group: str, include_subgroups: bool = True) -> list[dict[str, Any]]:
        encoded_group = quote(group, safe="")
        return await self.paginated_get(
            f"/groups/{encoded_group}/projects",
            params={
                "include_subgroups": str(include_subgroups).lower(),
                "archived": "false",
                "simple": "false",
            },
        )

    async def search_projects_by_namespace(self, namespace_path: str) -> list[dict[str, Any]]:
        """Find projects whose path_with_namespace starts with the given prefix.

        Uses the /projects API with search_namespaces=true as a fallback when
        the caller lacks group-level API access.
        """
        last_segment = namespace_path.rstrip("/").rsplit("/", 1)[-1]
        candidates = await self.paginated_get(
            "/projects",
            params={
                "search": last_segment,
                "search_namespaces": "true",
                "archived": "false",
                "simple": "false",
                "per_page": "100",
            },
        )
        prefix = namespace_path.strip("/").lower() + "/"
        exact = namespace_path.strip("/").lower()
        return [
            p for p in candidates
            if (p.get("path_with_namespace") or "").lower().startswith(prefix)
            or (p.get("path_with_namespace") or "").lower() == exact
        ]

    async def get_project(self, project: int | str) -> dict[str, Any]:
        encoded = self._encode_project(project)
        response = await self._request("GET", f"/projects/{encoded}")
        return response.json()

    async def get_project_file(self, project: int | str, file_path: str, ref: str) -> str:
        encoded_project = self._encode_project(project)
        encoded_file = quote(file_path, safe="")
        response = await self._request(
            "GET",
            f"/projects/{encoded_project}/repository/files/{encoded_file}/raw",
            params={"ref": ref},
        )
        return response.text

    async def list_repository_tree(
        self,
        project: int | str,
        ref: str,
        *,
        recursive: bool = True,
    ) -> list[dict[str, Any]]:
        encoded_project = self._encode_project(project)
        return await self.paginated_get(
            f"/projects/{encoded_project}/repository/tree",
            params={
                "ref": ref,
                "recursive": str(recursive).lower(),
                "per_page": "100",
            },
        )

    async def list_protected_branches(self, project: int | str) -> list[dict[str, Any]]:
        encoded = self._encode_project(project)
        return await self.paginated_get(f"/projects/{encoded}/protected_branches")

    async def get_project_approvals(self, project: int | str) -> dict[str, Any]:
        encoded = self._encode_project(project)
        response = await self._request("GET", f"/projects/{encoded}/approvals")
        return response.json()

    async def list_project_variables(self, project: int | str) -> list[dict[str, Any]]:
        encoded = self._encode_project(project)
        return await self.paginated_get(f"/projects/{encoded}/variables")

    async def get_project_push_rule(self, project: int | str) -> dict[str, Any] | None:
        encoded = self._encode_project(project)
        response = await self._request("GET", f"/projects/{encoded}/push_rule", expected=(200, 404))
        if response.status_code == 404:
            return None
        return response.json()

    async def lint_ci(self, project: int | str, content: str) -> dict[str, Any]:
        encoded = self._encode_project(project)
        response = await self._request(
            "POST",
            f"/projects/{encoded}/ci/lint",
            json={"content": content, "dry_run": False, "include_merged_yaml": True},
            expected=(200, 201),
        )
        return response.json()

    async def get_ci_template(self, template_name: str) -> str:
        encoded_template = quote(template_name, safe="")
        response = await self._request("GET", f"/templates/gitlab_ci_ymls/{encoded_template}")
        payload = response.json()
        content = payload.get("content")
        if not content:
            raise GitLabAPIError("Template response has no content", path=f"/templates/gitlab_ci_ymls/{template_name}")
        return content
