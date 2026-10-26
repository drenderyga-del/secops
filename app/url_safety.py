"""Fail-closed URL helpers for links sourced from persisted external data."""

from __future__ import annotations

from urllib.parse import urlsplit


def safe_external_http_url(value: object) -> str | None:
    """Return a clickable HTTP(S) URL, or ``None`` for unsafe legacy data."""
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if (
        not candidate
        or "\\" in candidate
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        return None

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.netloc.endswith(":")
    ):
        return None

    return candidate


def safe_evidence_http_url(value: object) -> str | None:
    """Project persisted GitLab evidence without credentials or query/fragment data."""
    candidate = safe_external_http_url(value)
    if candidate is None or len(candidate) > 2048:
        return None
    from .asoc_description_safety import _safe_http_url
    return _safe_http_url(candidate, strip_query=True, allow_name_segments=True)
