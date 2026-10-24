"""Operator-facing timezone shared by UI views and exported reports."""
from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_FALLBACK_TIMEZONE = "Europe/Moscow"


def _load_display_timezone() -> tuple[str, ZoneInfo]:
    name = os.getenv("UI_TIMEZONE") or os.getenv("TZ") or _FALLBACK_TIMEZONE
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("ui_timezone_invalid name=%s fallback=%s", name, _FALLBACK_TIMEZONE)
        return _FALLBACK_TIMEZONE, ZoneInfo(_FALLBACK_TIMEZONE)


DISPLAY_TIMEZONE_NAME, DISPLAY_TIMEZONE = _load_display_timezone()
