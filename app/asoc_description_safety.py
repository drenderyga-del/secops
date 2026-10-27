"""Safe projections of ASOC descriptions and ASOC public URLs.

Descriptions are untrusted upstream text. ScanOps only retains explicit scope
markers and validated HTTP(S) links; arbitrary description prose is not needed
for product identity and may contain credentials.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import unquote, urlsplit, urlunsplit

from .gitlab_url import _host_of, is_known_gitlab_host


_URL_RE = re.compile(r"https?://[^\s<>\[\]\"']+", re.IGNORECASE)
def _product_reference_hosts() -> set[str]:
    """Explicit host allowlist; deployment-specific directories are opt-in."""
    return {
        item.strip().casefold().rstrip(".")
        for item in os.getenv("ASOC_PRODUCT_REFERENCE_HOSTS", "").split(",")
        if item.strip()
    }


_SCOPE_RE = re.compile(
    r"^\s*scanops-scope\s*:\s*(internal|external)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CREDENTIAL_PATH_KEYS = {
    "accesskey",
    "accesstoken",
    "accesstokens",
    "apikey",
    "authorization",
    "bearer",
    "clientsecret",
    "credential",
    "credentials",
    "csrf",
    "deploytoken",
    "deploytokens",
    "hook",
    "hooks",
    "jobtoken",
    "jobtokens",
    "jwt",
    "oauth",
    "oauth2",
    "password",
    "passwd",
    "pat",
    "personalaccesstoken",
    "personalaccesstokens",
    "privatetoken",
    "privatetokens",
    "secret",
    "secrets",
    "sessionid",
    "sessiontoken",
    "signature",
    "sig",
    "token",
    "tokens",
    "webhook",
    "webhooksecret",
    "webhooks",
    "hooksecret",
    "xamzcredential",
    "xamzsecuritytoken",
    "xamzsignature",
    "xsrf",
    "xsrftoken",
}
_KNOWN_TOKEN_SEGMENT_RE = re.compile(
    r"^(?:glpat|glrt|glcbt|gldt|glsoat|gloas|glagent|glft|glptt)[-_][a-z0-9_-]{12,}$"
    r"|^github_pat_[a-z0-9_]{20,}$"
    r"|^gh[pousr]_[a-z0-9]{20,}$"
    r"|^xox[baprs]-[a-z0-9-]{12,}$",
    re.IGNORECASE,
)
_JWT_SEGMENT_RE = re.compile(r"^eyj[a-z0-9_-]{8,}\.eyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}$", re.IGNORECASE)
_HEX_SECRET_SEGMENT_RE = re.compile(r"^[a-f0-9]{32,}$", re.IGNORECASE)
_UUID_SEGMENT_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
_MAX_URL_DECODE_ROUNDS = 8
# GitLab reserves these top-level paths, so they are never a group or project;
# they host API, OAuth, profile and admin routes that can carry credentials.
_GITLAB_RESERVED_ROOTS = {"-", "api", "oauth", "profile", "admin"}
_PRODUCT_HOSTS_TTL_SECONDS = 60.0
_product_hosts_cache: tuple[float, frozenset[str]] = (float("-inf"), frozenset())


def _looks_like_token_segment(segment: str) -> bool:
    candidate = segment.strip().casefold()
    if not candidate or _UUID_SEGMENT_RE.fullmatch(candidate):
        return False
    if (
        _KNOWN_TOKEN_SEGMENT_RE.fullmatch(candidate)
        or _JWT_SEGMENT_RE.fullmatch(candidate)
        or _HEX_SECRET_SEGMENT_RE.fullmatch(candidate)
    ):
        return True
    if len(candidate) >= 40 and re.fullmatch(r"[a-z0-9_-]+", candidate):
        classes = sum((
            any(char.islower() for char in candidate),
            any(char.isupper() for char in segment),
            any(char.isdigit() for char in candidate),
            "_" in candidate or "-" in candidate,
        ))
        return classes >= 3
    return False


def _contains_credential_path(path: str, *, allow_name_segments: bool = False) -> bool:
    decoded = path
    for _ in range(_MAX_URL_DECODE_ROUNDS):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        # Deeply nested encoding is ambiguous across URL parsers and proxies.
        # Reject it instead of retaining a value that may reveal a secret only
        # after a later decoding pass.
        if unquote(decoded) != decoded:
            return True
    # Matrix parameters and encoded query-like delimiters can carry secrets
    # inside a path segment. Inspect every component after bounded decoding.
    for component in re.split(r"[/;?&#]", decoded):
        pieces = re.split(r"[=:]", component)
        key = pieces[0]
        named_key = re.sub(r"[^a-z0-9]+", "", key.casefold()) in _CREDENTIAL_PATH_KEYS
        # Plain credential words remain valid GitLab group/project names.
        if named_key and (not allow_name_segments or len(pieces) > 1):
            return True
        if any(_looks_like_token_segment(piece) for piece in pieces):
            return True
    return False


def _contains_credential_host(host: str) -> bool:
    """Reject credential-shaped host labels before persisting or exposing URLs."""
    for label in host.casefold().rstrip(".").split("."):
        compact = re.sub(r"[^a-z0-9]+", "", label)
        if compact in _CREDENTIAL_PATH_KEYS or _looks_like_token_segment(label):
            return True
    return False


def _safe_http_url(value: str, *, strip_query: bool, allow_name_segments: bool = False) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname or ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.netloc.endswith(":")
        or any(char.isspace() or ord(char) < 32 or char in "/?#@%\\" for char in host)
        or _contains_credential_host(host)
        or _contains_credential_path(parsed.path, allow_name_segments=allow_name_segments)
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value
    ):
        return None

    authority = host.casefold().rstrip(".")
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    query = "" if strip_query else parsed.query
    fragment = "" if strip_query else parsed.fragment
    return urlunsplit((parsed.scheme.casefold(), authority, parsed.path, query, fragment))


def _monitored_product_hosts() -> frozenset[str]:
    """GitLab hosts ScanOps already receives products from (cached briefly).

    Products are created from CI events, so their hosts are GitLab instances
    even when an instance is webhook-only and absent from the polling
    allowlist. This widens only link recognition, never where tokens are sent.
    """
    global _product_hosts_cache
    loaded_at, hosts = _product_hosts_cache
    now = time.monotonic()
    if now - loaded_at < _PRODUCT_HOSTS_TTL_SECONDS:
        return hosts
    try:
        from .models import Product, SessionLocal

        with SessionLocal() as db:
            hosts = frozenset(
                host
                for (base_url,) in db.query(Product.gitlab_base_url).distinct()
                if (host := _host_of(base_url or ""))
            )
    except Exception:  # noqa: BLE001 - keep the last known hosts if the DB is unavailable
        pass
    _product_hosts_cache = (now, hosts)
    return hosts


def is_description_gitlab_host(url: str) -> bool:
    host = _host_of(url)
    return bool(host) and (is_known_gitlab_host(url) or host in _monitored_product_hosts())


def _identity_part(value: str) -> str:
    """Drop GitLab sub-routes (``/-/tree/...``); the identity precedes them."""
    parsed = urlsplit(value)
    path = parsed.path
    marker = path.find("/-/")
    if marker >= 0:
        path = path[:marker] or "/"
    elif path.endswith("/-"):
        path = path[:-2] or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def safe_description_url(value: str) -> str | None:
    """Keep only GitLab identities/product references, with no secret paths."""
    try:
        candidate = _identity_part(value)
    except ValueError:
        return None
    strict = _safe_http_url(candidate, strip_query=True)
    host = (urlsplit(strict or candidate).hostname or "").casefold().rstrip(".")
    if host in _product_reference_hosts():
        return strict
    # GitLab identities: group/project words are names, not credential keys.
    safe = _safe_http_url(candidate, strip_query=True, allow_name_segments=True)
    if not safe:
        return None
    first_segment = next((segment for segment in urlsplit(safe).path.split("/") if segment), "")
    # A bare host or a reserved GitLab route is not a group/project identity.
    if not first_segment or first_segment.casefold() in _GITLAB_RESERVED_ROOTS:
        return None
    if is_description_gitlab_host(safe):
        return safe
    return None


def safe_description_links(description: str | None) -> list[dict[str, str]]:
    """Return only validated links safe to expose in JSON, HTML, and reports."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(description or ""):
        raw = match.group(0).rstrip(".,;:!?")
        while raw.endswith((")", "]", "}")):
            raw = raw[:-1]
        safe = safe_description_url(raw)
        if not safe or safe in seen:
            continue
        seen.add(safe)
        result.append({"url": safe, "kind": "gitlab" if is_description_gitlab_host(safe) else "reference"})
    return result


def description_reference_urls(description: str | None) -> list[str]:
    """Return raw HTTP(S) candidates for fail-closed identity matching."""
    result: list[str] = []
    for match in _URL_RE.finditer(description or ""):
        raw = match.group(0).rstrip(".,;:!?")
        while raw.endswith((")", "]", "}")):
            raw = raw[:-1]
        if raw and raw not in result:
            result.append(raw)
    return result


def sanitize_description(description: str | None) -> str | None:
    """Persist only scope markers and safe normalized links, never raw prose."""
    if not description:
        return None
    markers = list(dict.fromkeys(
        f"ScanOps-Scope: {value.casefold()}" for value in _SCOPE_RE.findall(description)
    ))
    links = [item["url"] for item in safe_description_links(description)]
    projection = markers + links
    return "\n".join(projection) or None


def safe_asoc_public_base(value: str | None) -> str | None:
    """Validate a configured ASOC public base; credentials/query data are rejected."""
    if not value:
        return None
    candidate = value.strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname or ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
        or parsed.netloc.endswith(":")
        or any(char.isspace() or ord(char) < 32 or char in "/?#@%\\" for char in host)
        or _contains_credential_host(host)
        or _contains_credential_path(parsed.path)
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in candidate)
        or "\\" in candidate
    ):
        return None
    authority = host.casefold().rstrip(".")
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    path = parsed.path.rstrip("/")
    if path.casefold().endswith("/api"):
        path = path[:-4]
    return urlunsplit((parsed.scheme.casefold(), authority, path.rstrip("/"), "", "")) or None


def sanitize_existing_asoc_descriptions(engine) -> None:
    """One-time data migration removing untrusted legacy ASOC description text."""
    from sqlalchemy import text

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "SELECT set_config("
                    "'scanops.asoc_product_lifecycle_write', 'allowed', true)"
                )
            )
        rows = connection.execute(
            text("SELECT id, description FROM asoc_products WHERE description IS NOT NULL")
        ).fetchall()
        for row_id, description in rows:
            sanitized = sanitize_description(description)
            if sanitized != description:
                connection.execute(
                    text("UPDATE asoc_products SET description = :description WHERE id = :id"),
                    {"description": sanitized, "id": row_id},
                )
