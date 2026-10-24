"""Canonical outcome for an asynchronous PT AI/ASOC scan."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PtaiOutcome:
    canonical_status: str
    failure_source: str | None = None
    reason: str | None = None


def resolve_ptai_outcome(scan: Any) -> PtaiOutcome:
    """Resolve one user-facing outcome while preserving raw scan provenance."""
    asoc = str(getattr(scan, "asoc_status", "") or "").strip().upper()
    if bool(getattr(scan, "trigger_failed", False)) or asoc == "TRIGGER_FAILED":
        return PtaiOutcome("ERROR", "asoc_trigger", "ASOC did not accept the scan trigger")
    if asoc in {"ERROR", "FAILED", "FAILURE", "ABORTED", "CANCELED", "CANCELLED"}:
        return PtaiOutcome("ERROR", "asoc", getattr(scan, "error_text", None) or "ASOC scan failed")

    engine_stage = str(getattr(scan, "ptai_stage", "") or "").strip().lower()
    engine_error = bool(getattr(scan, "ptai_has_errors", False)) or bool(getattr(scan, "ptai_error", None))
    if engine_stage in {"failed", "aborted"}:
        return PtaiOutcome("ERROR", "ptai_engine", getattr(scan, "ptai_error", None) or "PT AI engine failed")
    if asoc in {"PENDING", "QUEUED", "ENQUEUED"}:
        return PtaiOutcome("PENDING")
    if asoc in {"IN_PROGRESS", "PROGRESS", "RUNNING", "SCAN"}:
        return PtaiOutcome("IN_PROGRESS")
    if asoc == "PARTIAL_SUCCESS":
        return PtaiOutcome("PARTIAL_SUCCESS", "asoc", getattr(scan, "error_text", None) or "ASOC reported a partial result")
    if engine_error and (asoc in {"SUCCESS", "SUCCEEDED", "DONE"} or engine_stage in {"done", "completed", "success", "succeeded"}):
        return PtaiOutcome("PARTIAL_SUCCESS", "ptai_engine", getattr(scan, "ptai_error", None) or "PT AI reported engine errors")
    if asoc in {"SUCCESS", "SUCCEEDED", "DONE"} or engine_stage in {"done", "completed", "success", "succeeded"}:
        return PtaiOutcome("SUCCESS")
    if asoc in {"IN_PROGRESS", "PROGRESS", "RUNNING", "SCAN"} or engine_stage in {"scan", "running", "analysis", "processing"}:
        return PtaiOutcome("IN_PROGRESS")
    return PtaiOutcome("PENDING")
