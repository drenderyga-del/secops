"""Canonical BI_ID parsing shared by product integrations."""

from __future__ import annotations

import re
from typing import Any


_BI_PART_RE = re.compile(r"(?:BI[\s_-]*)?[0-9]+(?:\.0+)?", re.IGNORECASE)


def normalize_bi_id(value: Any) -> str | None:
    """Return a canonical BI identity, including slash/comma composites.

    Only explicit numeric identifiers are accepted. Integral spreadsheet
    decimals are tolerated; arbitrary prose and fractional identifiers are
    rejected. The compact sorted result makes equivalent composites equal.
    """
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if not text:
        return None
    parts = re.split(r"\s*[/,]\s*", text)
    if not parts or any(not _BI_PART_RE.fullmatch(part.strip()) for part in parts):
        return None
    canonical_tokens: set[str] = set()
    for part in parts:
        match = re.fullmatch(
            r"(?:BI[\s_-]*)?([0-9]+)(?:\.0+)?",
            part.strip(),
            re.IGNORECASE,
        )
        if match is None:
            return None
        canonical_tokens.add(match.group(1).lstrip("0") or "0")
    return "/".join(sorted(canonical_tokens, key=lambda token: (len(token), token)))
