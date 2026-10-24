from types import SimpleNamespace

from app.ptai_outcome import resolve_ptai_outcome


def test_asoc_failure_wins_after_successful_ptai_engine():
    outcome = resolve_ptai_outcome(SimpleNamespace(asoc_status="FAILED", ptai_stage="Done"))
    assert (outcome.canonical_status, outcome.failure_source) == ("ERROR", "asoc")


def test_engine_failure_is_error():
    outcome = resolve_ptai_outcome(SimpleNamespace(asoc_status="SUCCESS", ptai_stage="Failed"))
    assert (outcome.canonical_status, outcome.failure_source) == ("ERROR", "ptai_engine")


def test_engine_errors_after_done_are_partial():
    outcome = resolve_ptai_outcome(SimpleNamespace(asoc_status="SUCCESS", ptai_stage="Done", ptai_has_errors=True))
    assert (outcome.canonical_status, outcome.failure_source) == ("PARTIAL_SUCCESS", "ptai_engine")


def test_missing_optional_engine_does_not_block_asoc_success():
    assert resolve_ptai_outcome(SimpleNamespace(asoc_status="SUCCESS")).canonical_status == "SUCCESS"


def test_asoc_in_progress_wins_over_completed_engine():
    outcome = resolve_ptai_outcome(SimpleNamespace(asoc_status="IN_PROGRESS", ptai_stage="Done"))
    assert outcome.canonical_status == "IN_PROGRESS"


def test_engine_failure_wins_over_pending_asoc():
    outcome = resolve_ptai_outcome(SimpleNamespace(asoc_status="PENDING", ptai_stage="Failed"))
    assert (outcome.canonical_status, outcome.failure_source) == ("ERROR", "ptai_engine")


def test_pending_is_queued_and_in_progress_is_running():
    assert resolve_ptai_outcome(SimpleNamespace(asoc_status="PENDING")).canonical_status == "PENDING"
    assert resolve_ptai_outcome(SimpleNamespace(asoc_status="IN_PROGRESS")).canonical_status == "IN_PROGRESS"


def test_engine_error_without_stage_is_partial_after_asoc_success():
    outcome = resolve_ptai_outcome(SimpleNamespace(asoc_status="SUCCESS", ptai_has_errors=True, ptai_stage=None))
    assert (outcome.canonical_status, outcome.failure_source) == ("PARTIAL_SUCCESS", "ptai_engine")


def test_ptai_result_url_requires_project_for_default_route(monkeypatch):
    from app.ptai_client import ptai_result_url

    monkeypatch.setenv("PTAI_URL", "https://ptai.example")
    monkeypatch.delenv("PTAI_RESULT_URL_TEMPLATE", raising=False)
    scan = SimpleNamespace(ptai_project_id=None, ptai_branch_id="branch-1", ptai_scan_result_id="result-1")
    assert ptai_result_url(scan) is None
