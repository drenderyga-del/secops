"""Contract tests for Pipelines lifecycle grouping and filter vocabulary."""

import pytest

from app.models.pipeline import PipelineStatus
from app.pipeline_view_contract import (
    DEFAULT_PIPELINE_GROUP,
    DEFAULT_PIPELINE_TIMEFRAME,
    PIPELINE_STATUS_GROUP,
    PIPELINE_TIMEFRAMES,
    PipelineLifecycleGroup,
    group_for_pipeline_status,
)


def test_every_stored_pipeline_status_has_exactly_one_summary_group():
    assert set(PIPELINE_STATUS_GROUP) == set(PipelineStatus)
    assert all(isinstance(group, PipelineLifecycleGroup) for group in PIPELINE_STATUS_GROUP.values())


def test_pipeline_status_grouping_matches_the_product_decision():
    assert PIPELINE_STATUS_GROUP == {
        PipelineStatus.REGISTERED: PipelineLifecycleGroup.IN_PROGRESS,
        PipelineStatus.POLLING: PipelineLifecycleGroup.IN_PROGRESS,
        PipelineStatus.COMPLETED: PipelineLifecycleGroup.COMPLETED_RECENTLY,
        PipelineStatus.FAILED: PipelineLifecycleGroup.NEEDS_ATTENTION,
        PipelineStatus.STALE: PipelineLifecycleGroup.NEEDS_ATTENTION,
    }
    assert {group.value for group in PipelineLifecycleGroup} == {
        "needs_attention",
        "in_progress",
        "completed_recently",
    }


def test_pipeline_filter_defaults_and_timeframes_are_stable():
    assert DEFAULT_PIPELINE_GROUP is PipelineLifecycleGroup.IN_PROGRESS
    assert DEFAULT_PIPELINE_TIMEFRAME == "7d"
    assert PIPELINE_TIMEFRAMES == ("today", "3d", "7d", "30d", "1y", "all")


def test_group_lookup_accepts_enum_or_stored_string_and_fails_closed():
    assert group_for_pipeline_status(PipelineStatus.STALE) is PipelineLifecycleGroup.NEEDS_ATTENTION
    assert group_for_pipeline_status("completed") is PipelineLifecycleGroup.COMPLETED_RECENTLY

    with pytest.raises(ValueError, match="Unknown pipeline status"):
        group_for_pipeline_status("brand-new-status")
