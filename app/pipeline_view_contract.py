"""Executable product contract for the Pipelines list.

The query and UI work tracked by backlog items 308 and 309 must consume these
values instead of maintaining separate status-to-group mappings.
"""

from __future__ import annotations

from enum import Enum

from .models.pipeline import PipelineStatus


class PipelineLifecycleGroup(str, Enum):
    NEEDS_ATTENTION = "needs_attention"
    IN_PROGRESS = "in_progress"
    COMPLETED_RECENTLY = "completed_recently"


PIPELINE_STATUS_GROUP: dict[PipelineStatus, PipelineLifecycleGroup] = {
    PipelineStatus.REGISTERED: PipelineLifecycleGroup.IN_PROGRESS,
    PipelineStatus.POLLING: PipelineLifecycleGroup.IN_PROGRESS,
    PipelineStatus.COMPLETED: PipelineLifecycleGroup.COMPLETED_RECENTLY,
    PipelineStatus.FAILED: PipelineLifecycleGroup.NEEDS_ATTENTION,
    PipelineStatus.STALE: PipelineLifecycleGroup.NEEDS_ATTENTION,
}

DEFAULT_PIPELINE_GROUP = PipelineLifecycleGroup.IN_PROGRESS

# Reuse the observation-window vocabulary already exposed by dashboard and
# execution routes.  "Recently" is defined by the selected value.
PIPELINE_TIMEFRAMES = ("today", "3d", "7d", "30d", "1y", "all")
DEFAULT_PIPELINE_TIMEFRAME = "7d"


def group_for_pipeline_status(status: PipelineStatus | str) -> PipelineLifecycleGroup:
    """Return the single lifecycle group for a stored pipeline status.

    Unknown values fail closed so a newly added database status cannot silently
    disappear from summary counts.
    """

    try:
        normalized = status if isinstance(status, PipelineStatus) else PipelineStatus(status)
    except ValueError as exc:
        raise ValueError(f"Unknown pipeline status: {status}") from exc
    return PIPELINE_STATUS_GROUP[normalized]
