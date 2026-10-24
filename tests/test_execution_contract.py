"""Scanner-agnostic validation for the shared ScanOps execution contract."""

import pytest

from app.execution_contract import (
    ExecutionStatus,
    FailureCategory,
    FailureReason,
    LegacySource,
    OperationalCondition,
    StepStatus,
    TERMINAL_EXECUTION_STATUSES,
    can_transition,
    category_for_reason,
    map_legacy_status,
)


def test_lifecycle_has_one_terminal_vocabulary_for_every_adapter():
    assert {status.value for status in TERMINAL_EXECUTION_STATUSES} == {
        "succeeded",
        "partial",
        "failed",
        "cancelled",
    }
    assert set(TERMINAL_EXECUTION_STATUSES) < set(ExecutionStatus)


@pytest.mark.parametrize("terminal", TERMINAL_EXECUTION_STATUSES)
def test_terminal_state_cannot_regress(terminal):
    assert can_transition(terminal, terminal)
    for target in set(ExecutionStatus) - {terminal}:
        assert not can_transition(terminal, target)


def test_delivery_and_cooperative_cancellation_are_explicit_phases():
    assert can_transition(ExecutionStatus.RUNNING, ExecutionStatus.DELIVERING)
    assert can_transition(ExecutionStatus.RUNNING, ExecutionStatus.CANCEL_REQUESTED)
    assert can_transition(ExecutionStatus.CANCEL_REQUESTED, ExecutionStatus.CANCELLED)
    assert not can_transition(ExecutionStatus.DELIVERING, ExecutionStatus.RUNNING)


@pytest.mark.parametrize(
    ("source", "raw", "expected"),
    [
        (LegacySource.IAC_RUN, "partial", ExecutionStatus.PARTIAL),
        (LegacySource.CONFIG_SCAN, "failed", ExecutionStatus.FAILED),
        (LegacySource.PTAI_ASOC, "PARTIAL_SUCCESS", ExecutionStatus.PARTIAL),
        (LegacySource.PTAI_ASOC, "TRIGGER_FAILED", ExecutionStatus.FAILED),
    ],
)
def test_current_execution_flows_use_the_same_canonical_states(source, raw, expected):
    assert map_legacy_status(source, raw).execution_status is expected


def test_pipeline_stale_is_a_condition_not_a_terminal_status():
    observation = map_legacy_status(LegacySource.GITLAB_PIPELINE, "stale")
    assert observation.execution_status is None
    assert observation.condition is OperationalCondition.STALE


@pytest.mark.parametrize(
    ("source", "raw", "step_status", "result_code"),
    [
        (LegacySource.GITLAB_JOB, "success", StepStatus.SUCCEEDED, None),
        (LegacySource.GITLAB_PIPELINE, "completed", None, "polling_completed"),
        (LegacySource.IAC_RUN, "succeeded", None, "legacy_scan_succeeded"),
    ],
)
def test_legacy_completion_does_not_bypass_the_control_contract(
    source, raw, step_status, result_code
):
    observation = map_legacy_status(source, raw)
    assert observation.execution_status is None
    assert observation.step_status is step_status
    assert observation.result_code == result_code


@pytest.mark.parametrize(
    ("source", "raw", "result_code"),
    [
        (LegacySource.GITLAB_JOB, "skipped", "skipped"),
        (LegacySource.IAC_SCAN, "no_iac", "no_iac"),
        (LegacySource.ASOC_DELIVERY, "skipped", "delivery_skipped"),
    ],
)
def test_policy_dependent_outcomes_do_not_claim_execution_success(source, raw, result_code):
    observation = map_legacy_status(source, raw)
    assert observation.execution_status is None
    assert observation.step_status is StepStatus.SKIPPED
    assert observation.result_code == result_code


def test_asoc_upload_is_a_delivery_step_not_scanner_success():
    pending = map_legacy_status(LegacySource.ASOC_DELIVERY, "pending")
    uploaded = map_legacy_status(LegacySource.ASOC_DELIVERY, "uploaded")
    assert pending.execution_status is ExecutionStatus.DELIVERING
    assert uploaded.execution_status is None
    assert uploaded.step_status is StepStatus.SUCCEEDED


@pytest.mark.parametrize(
    "raw",
    ["covered", "pipeline_no_sec", "no_pipelines", "inaccessible", "excluded"],
)
def test_audit_coverage_is_not_an_execution(raw):
    observation = map_legacy_status(LegacySource.AUDIT_COVERAGE, raw)
    assert not observation.is_execution_evidence
    assert observation.execution_status is None


def test_every_failure_reason_has_exactly_one_stable_category():
    grouped = {reason: category_for_reason(reason) for reason in FailureReason}
    assert set(grouped) == set(FailureReason)
    assert grouped[FailureReason.MISSING_CREDENTIAL] is FailureCategory.AUTHENTICATION
    assert grouped[FailureReason.WORKER_INTERRUPTED] is FailureCategory.ORCHESTRATION
    assert grouped[FailureReason.ARTIFACT_EMPTY] is FailureCategory.ARTIFACT
    assert grouped[FailureReason.CONFIRMATION_MISSING] is FailureCategory.DELIVERY


def test_unknown_adapter_status_fails_closed():
    with pytest.raises(ValueError, match="Unknown ptai_engine status"):
        map_legacy_status(LegacySource.PTAI_ENGINE, "brand-new-vendor-state")
