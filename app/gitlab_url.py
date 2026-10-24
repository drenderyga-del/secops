"""Small helpers for GitLab URL handling (no heavy deps)."""

import json
from urllib.parse import urlparse

from .integration_settings import runtime_setting


def _host_of(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = f"https://{u}"
    return (urlparse(u).netloc or "").lower().split("@")[-1].split(":")[0]


def known_gitlab_hosts() -> set[str]:
    """Hosts the monitor is configured to talk to: GITLAB_API_URL + GITLAB_INSTANCES.

    Used as an allowlist so a scan target (an operator-supplied URL) can never
    make the worker send GITLAB_PAT to an arbitrary host (SSRF / token leak).
    """
    hosts: set[str] = set()
    default_host = _host_of(runtime_setting("GITLAB_API_URL", ""))
    if default_host:
        hosts.add(default_host)
    raw = runtime_setting("GITLAB_INSTANCES", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        entries = data if isinstance(data, list) else (list(data.keys()) if isinstance(data, dict) else [])
        for entry in entries:
            host = entry.get("host") if isinstance(entry, dict) else entry
            host = _host_of(str(host or ""))
            if host:
                hosts.add(host)
    return hosts


def is_known_gitlab_host(url: str) -> bool:
    host = _host_of(url)
    return bool(host) and host in known_gitlab_hosts()


def normalize_gitlab_origin(url: str) -> str:
    """Strip trailing /api/v4 so API clients can append /api/v4 exactly once.

    GITLAB_API_URL and instance api_url values often already include /api/v4.
    """
    u = (url or "").strip().rstrip("/")
    low = u.lower()
    suffix = "/api/v4"
    if low.endswith(suffix):
        u = u[: -len(suffix)].rstrip("/")
    return u


def gitlab_web_origin(url: str) -> str:
    """Host-only URL for GitLabScanner (scheme + netloc). Strips path and /api/v4.

    Product.gitlab_base_url may be a full project URL; poller api_url may include /api/v4.
    """
    u = normalize_gitlab_origin((url or "").strip())
    if not u:
        return ""
    if "://" not in u:
        u = f"https://{u}"
    parsed = urlparse(u)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def looks_like_gitlab_web_url(url: str) -> bool:
    """True only for http(s) URLs with a host. Bare paths like group/sub are False."""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return False
    return bool(urlparse(u).netloc)
