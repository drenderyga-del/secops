#!/usr/bin/env python3
"""Fail-closed validation for local browser-gate HTTP endpoints."""

from __future__ import annotations

import argparse
import ipaddress
from urllib.parse import urlparse


def validate_loopback_http_url(value: str, *, name: str) -> str:
    """Return a normalized URL only when it targets an explicit loopback host."""
    try:
        parsed = urlparse(value.strip())
        host = parsed.hostname
        is_loopback = host == "localhost"
        if host and not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
    except ValueError:
        parsed = None
        is_loopback = False

    if (
        parsed is None
        or parsed.scheme != "http"
        or not parsed.netloc
        or not is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{name} must be an http:// loopback URL without credentials, "
            "path, query, or fragment"
        )
    return value.strip().rstrip("/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        validate_loopback_http_url(args.url, name=args.name)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
