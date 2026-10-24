"""Shared ScanOps execution lifecycle and failure taxonomy.

This module is the implementation contract for adapters.  It intentionally
does not persist unified executions yet: database rollout belongs to the
separate Unified Execution task.  Existing GitLab, IaC, config scan, PT AI and
ASOC code can import these values without inventing source-specific core
states.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionStatus(str, Enum):
    REQUESTED = "requested"
    QUEUED = "queued"
    RUNNING = "running"
    DELIVERING = "delivering"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    REQUESTED = "requested"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class OperationalCondition(str, Enum):
    STALLED = "stalled"
    STALE = "stale"
    DELAYED = "delayed"


class FailureCategory(str, Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    CAPACITY = "capacity"
    ORCHESTRATION = "orchestration"
    EXECUTION = "execution"
    ARTIFACT = "artifact"
    DELIVERY = "delivery"
    DATA_INTEGRITY = "data_integrity"


class FailureReason(str, Enum):
    MISSING_ENDPOINT = "missing_endpoint"
    INVALID_PROFILE = "invalid_profile"
    UNSUPPORTED_REF = "unsupported_ref"
    MISSING_BINDING = "missing_binding"
    MISSING_CREDENTIAL = "missing_credential"
    CREDENTIAL_REJECTED = "credential_rejected"
    CREDENTIAL_EXPIRED = "credential_expired"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    DNS_FAILED = "dns_failed"
    CONNECTION_REFUSED = "connection_refused"
    TLS_FAILED = "tls_failed"
    REQUEST_TIMEOUT = "request_timeout"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    QUEUE_SATURATED = "queue_saturated"
    NO_COMPATIBLE_WORKER = "no_compatible_worker"
    WORKER_UNAVAILABLE = "worker_unavailable"
    CONCURRENCY_LIMIT = "concurrency_limit"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    TRIGGER_REJECTED = "trigger_rejected"
    MISSING_CORRELATION_ID = "missing_correlation_id"
    POLL_FAILED = "poll_failed"
    INVALID_TRANSITION = "invalid_transition"
    WORKER_INTERRUPTED = "worker_interrupted"
    SCANNER_FAILED = "scanner_failed"
    SCANNER_TIMEOUT = "scanner_timeout"
    CANCELLED_BY_SOURCE = "cancelled_by_source"
    INVALID_OUTPUT = "invalid_output"
    PARTIAL_TARGET_FAILURE = "partial_target_failure"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_EMPTY = "artifact_empty"
    ARTIFACT_UNREADABLE = "artifact_unreadable"
    ARTIFACT_SCHEMA_INVALID = "artifact_schema_invalid"
    DESTINATION_MISSING = "destination_missing"
    UPLOAD_REJECTED = "upload_rejected"
    UPLOAD_FAILED = "upload_failed"
    DELIVERY_TIMEOUT = "delivery_timeout"
    CONFIRMATION_MISSING = "confirmation_missing"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    TARGET_MISMATCH = "target_mismatch"
    REF_MISMATCH = "ref_mismatch"
    CORRELATION_CONFLICT = "correlation_conflict"
    DUPLICATE_EVENT = "duplicate_event"


class LegacySource(str, Enum):
    GITLAB_PIPELINE = "gitlab_pipeline"
    GITLAB_JOB = "gitlab_job"
    IAC_RUN = "iac_run"
    IAC_FINDER = "iac_finder"
    IAC_SCAN = "iac_scan"
    ASOC_DELIVERY = "asoc_delivery"
    CONFIG_SCAN = "config_scan"
    PTAI_ASOC = "ptai_asoc"
    PTAI_ENGINE = "ptai_engine"
    AUDIT_COVERAGE = "audit_coverage"


@dataclass(frozen=True)
class CanonicalObservation:
    """Canonical meaning of one raw legacy status.

    ``execution_status`` is deliberately absent for coverage observations,
    derived conditions and policy-dependent step outcomes such as ``skipped``
    or ``no_iac``.  Adapters must resolve those against the control contract
    instead of declaring an end-to-end success implicitly.
    """

    execution_status: ExecutionStatus | None = None
    step_status: StepStatus | None = None
    condition: OperationalCondition | None = None
    result_code: str | None = None
    is_execution_evidence: bool = True


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.PARTIAL,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    }
)


_ALLOWED_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.REQUESTED: frozenset(
        {
            ExecutionStatus.QUEUED,
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.PARTIAL,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
    ),
    ExecutionStatus.QUEUED: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.PARTIAL,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.DELIVERING,
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.PARTIAL,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
    ),
    ExecutionStatus.DELIVERING: frozenset(
        {
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.PARTIAL,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
    ),
    # Completion may win a race with cooperative cancellation.
    ExecutionStatus.CANCEL_REQUESTED: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.PARTIAL,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
    ),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.PARTIAL: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
}


def can_transition(
    current: ExecutionStatus | None,
    target: ExecutionStatus,
) -> bool:
    """Return whether a canonical transition is valid and non-regressive."""

    if current is None or current == target:
        return True
    return target in _ALLOWED_TRANSITIONS[current]


_ALLOWED_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.REQUESTED: frozenset(
        {
            StepStatus.QUEUED,
            StepStatus.RUNNING,
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
            StepStatus.SKIPPED,
        }
    ),
    StepStatus.QUEUED: frozenset(
        {
            StepStatus.RUNNING,
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
            StepStatus.SKIPPED,
        }
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
            StepStatus.SKIPPED,
        }
    ),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
}


def can_step_transition(current: StepStatus | None, target: StepStatus) -> bool:
    """Return whether a scanner step can advance without regressing."""

    if current is None or current == target:
        return True
    return target in _ALLOWED_STEP_TRANSITIONS[current]


_REASONS_BY_CATEGORY: dict[FailureCategory, frozenset[FailureReason]] = {
    FailureCategory.CONFIGURATION: frozenset(
        {
            FailureReason.MISSING_ENDPOINT,
            FailureReason.INVALID_PROFILE,
            FailureReason.UNSUPPORTED_REF,
            FailureReason.MISSING_BINDING,
        }
    ),
    FailureCategory.AUTHENTICATION: frozenset(
        {
            FailureReason.MISSING_CREDENTIAL,
            FailureReason.CREDENTIAL_REJECTED,
            FailureReason.CREDENTIAL_EXPIRED,
            FailureReason.INSUFFICIENT_SCOPE,
        }
    ),
    FailureCategory.CONNECTIVITY: frozenset(
        {
            FailureReason.DNS_FAILED,
            FailureReason.CONNECTION_REFUSED,
            FailureReason.TLS_FAILED,
            FailureReason.REQUEST_TIMEOUT,
            FailureReason.UPSTREAM_UNREACHABLE,
        }
    ),
    FailureCategory.CAPACITY: frozenset(
        {
            FailureReason.QUEUE_SATURATED,
            FailureReason.NO_COMPATIBLE_WORKER,
            FailureReason.WORKER_UNAVAILABLE,
            FailureReason.CONCURRENCY_LIMIT,
            FailureReason.RESOURCE_EXHAUSTED,
        }
    ),
    FailureCategory.ORCHESTRATION: frozenset(
        {
            FailureReason.TRIGGER_REJECTED,
            FailureReason.MISSING_CORRELATION_ID,
            FailureReason.POLL_FAILED,
            FailureReason.INVALID_TRANSITION,
            FailureReason.WORKER_INTERRUPTED,
        }
    ),
    FailureCategory.EXECUTION: frozenset(
        {
            FailureReason.SCANNER_FAILED,
            FailureReason.SCANNER_TIMEOUT,
            FailureReason.CANCELLED_BY_SOURCE,
            FailureReason.INVALID_OUTPUT,
            FailureReason.PARTIAL_TARGET_FAILURE,
        }
    ),
    FailureCategory.ARTIFACT: frozenset(
        {
            FailureReason.ARTIFACT_MISSING,
            FailureReason.ARTIFACT_EMPTY,
            FailureReason.ARTIFACT_UNREADABLE,
            FailureReason.ARTIFACT_SCHEMA_INVALID,
        }
    ),
    FailureCategory.DELIVERY: frozenset(
        {
            FailureReason.DESTINATION_MISSING,
            FailureReason.UPLOAD_REJECTED,
            FailureReason.UPLOAD_FAILED,
            FailureReason.DELIVERY_TIMEOUT,
            FailureReason.CONFIRMATION_MISSING,
            FailureReason.DUPLICATE_DELIVERY,
        }
    ),
    FailureCategory.DATA_INTEGRITY: frozenset(
        {
            FailureReason.TARGET_MISMATCH,
            FailureReason.REF_MISMATCH,
            FailureReason.CORRELATION_CONFLICT,
            FailureReason.DUPLICATE_EVENT,
        }
    ),
}


def category_for_reason(reason: FailureReason) -> FailureCategory:
    """Resolve a stable failure category for a reason code."""

    for category, reasons in _REASONS_BY_CATEGORY.items():
        if reason in reasons:
            return category
    raise ValueError(f"Unregistered failure reason: {reason}")


_S = ExecutionStatus
_SS = StepStatus
_C = OperationalCondition
_O = CanonicalObservation

_LEGACY_STATUS_MAP: dict[LegacySource, dict[str, CanonicalObservation]] = {
    LegacySource.GITLAB_PIPELINE: {
        "registered": _O(execution_status=_S.REQUESTED),
        "polling": _O(execution_status=_S.RUNNING),
        # Polling finished, but tracked security jobs may still have failed.
        "completed": _O(result_code="polling_completed"),
        "failed": _O(execution_status=_S.FAILED),
        "stale": _O(condition=_C.STALE),
    },
    LegacySource.GITLAB_JOB: {
        # A GitLab job can be a control execution or only a trigger/step (PT AI).
        "created": _O(step_status=_SS.REQUESTED),
        "pending": _O(step_status=_SS.QUEUED),
        "waiting_for_resource": _O(step_status=_SS.QUEUED),
        "running": _O(step_status=_SS.RUNNING),
        "success": _O(step_status=_SS.SUCCEEDED),
        "failed": _O(step_status=_SS.FAILED),
        "canceled": _O(step_status=_SS.CANCELLED),
        "cancelled": _O(step_status=_SS.CANCELLED),
        "skipped": _O(step_status=_SS.SKIPPED, result_code="skipped"),
        "manual": _O(step_status=_SS.SKIPPED, result_code="manual_not_started"),
    },
    LegacySource.IAC_RUN: {
        "queued": _O(execution_status=_S.QUEUED),
        "running": _O(execution_status=_S.RUNNING),
        # Current verdict does not include mandatory ASOC delivery policy.
        "succeeded": _O(result_code="legacy_scan_succeeded"),
        "partial": _O(execution_status=_S.PARTIAL),
        "failed": _O(execution_status=_S.FAILED),
        "cancelled": _O(execution_status=_S.CANCELLED),
    },
    LegacySource.IAC_FINDER: {
        "queued": _O(step_status=_SS.QUEUED),
        "running": _O(step_status=_SS.RUNNING),
        "succeeded": _O(step_status=_SS.SUCCEEDED),
        "failed": _O(step_status=_SS.FAILED),
    },
    LegacySource.IAC_SCAN: {
        "queued": _O(step_status=_SS.QUEUED),
        "running": _O(step_status=_SS.RUNNING),
        "succeeded": _O(step_status=_SS.SUCCEEDED),
        "failed": _O(step_status=_SS.FAILED),
        "no_iac": _O(step_status=_SS.SKIPPED, result_code="no_iac"),
    },
    LegacySource.ASOC_DELIVERY: {
        "pending": _O(execution_status=_S.DELIVERING, step_status=_SS.QUEUED),
        "uploaded": _O(step_status=_SS.SUCCEEDED),
        "failed": _O(step_status=_SS.FAILED),
        "skipped": _O(step_status=_SS.SKIPPED, result_code="delivery_skipped"),
    },
    LegacySource.CONFIG_SCAN: {
        "queued": _O(execution_status=_S.QUEUED),
        "running": _O(execution_status=_S.RUNNING),
        "succeeded": _O(execution_status=_S.SUCCEEDED),
        "failed": _O(execution_status=_S.FAILED),
    },
    LegacySource.PTAI_ASOC: {
        "pending": _O(execution_status=_S.QUEUED),
        "in_progress": _O(execution_status=_S.RUNNING),
        "success": _O(execution_status=_S.SUCCEEDED),
        "partial_success": _O(execution_status=_S.PARTIAL),
        "error": _O(execution_status=_S.FAILED),
        "trigger_failed": _O(execution_status=_S.FAILED),
    },
    LegacySource.PTAI_ENGINE: {
        "queued": _O(step_status=_SS.QUEUED),
        "enqueued": _O(step_status=_SS.QUEUED),
        "scan": _O(step_status=_SS.RUNNING),
        "running": _O(step_status=_SS.RUNNING),
        "done": _O(step_status=_SS.SUCCEEDED),
        "failed": _O(step_status=_SS.FAILED),
        "aborted": _O(step_status=_SS.FAILED),
    },
    LegacySource.AUDIT_COVERAGE: {
        status: _O(result_code=status, is_execution_evidence=False)
        for status in (
            "covered",
            "pipeline_no_sec",
            "no_pipelines",
            "inaccessible",
            "excluded",
        )
    },
}


def map_legacy_status(source: LegacySource, raw_status: str) -> CanonicalObservation:
    """Map current source state to the shared contract without guessing policy.

    Unknown values fail closed.  They must be added explicitly with a contract
    test so a scanner upgrade cannot silently change lifecycle semantics.
    """

    normalized = str(raw_status).strip().replace("-", "_").replace(" ", "_").lower()
    try:
        return _LEGACY_STATUS_MAP[source][normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown {source.value} status: {raw_status!r}") from exc
