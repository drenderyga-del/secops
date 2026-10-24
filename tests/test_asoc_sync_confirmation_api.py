from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.asoc_products import AsocSyncConfirmationRequiredError
from app.routers.web import signal_control


def test_sync_endpoint_returns_safe_confirmation_contract(monkeypatch):
    expected_version = "a" * 64

    marks = []

    def _confirmation_required(_db, **kwargs):
        assert kwargs["confirm_mass_removal"] is False
        marks.append(datetime.now(timezone.utc))
        raise AsocSyncConfirmationRequiredError(
            active_count=8,
            incoming_count=1,
            removal_count=7,
            registry_version=expected_version,
        )

    outcomes = []
    db = SimpleNamespace(rolled_back=False)
    db.rollback = lambda: setattr(db, "rolled_back", True)
    monkeypatch.setattr(signal_control, "fetch_and_sync", _confirmation_required)
    monkeypatch.setattr(
        signal_control,
        "record_sync_outcome",
        lambda _db, **kwargs: outcomes.append(kwargs),
    )

    with pytest.raises(HTTPException) as raised:
        signal_control.signal_control_asoc_sync(
            payload=signal_control.AsocSyncRequest(),
            db=db,
        )

    assert db.rolled_back is True
    assert [(outcome["trigger"], outcome["status"]) for outcome in outcomes] == [
        ("operator", "needs_confirmation")
    ]
    # Outcomes are keyed to when the attempt started, not when it was written.
    assert outcomes[0]["at"] <= marks[0]
    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "code": "confirmation_required",
        "message": "This snapshot would archive an unusually large part of the ASOC registry.",
        "active_count": 8,
        "incoming_count": 1,
        "removal_count": 7,
        "registry_version": expected_version,
    }


def test_failed_outcome_write_does_not_replace_the_confirmation_prompt(monkeypatch):
    def _confirmation_required(_db, **_kwargs):
        raise AsocSyncConfirmationRequiredError(
            active_count=8,
            incoming_count=1,
            removal_count=7,
            registry_version="b" * 64,
        )

    def _state_write_fails(_db, **_kwargs):
        raise RuntimeError("database unavailable")

    db = SimpleNamespace(rollbacks=0)
    db.rollback = lambda: setattr(db, "rollbacks", db.rollbacks + 1)
    monkeypatch.setattr(signal_control, "fetch_and_sync", _confirmation_required)
    monkeypatch.setattr(signal_control, "record_sync_outcome", _state_write_fails)

    with pytest.raises(HTTPException) as raised:
        signal_control.signal_control_asoc_sync(payload=signal_control.AsocSyncRequest(), db=db)

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "confirmation_required"


def test_sync_request_rejects_negative_expected_counts():
    with pytest.raises(ValueError):
        signal_control.AsocSyncRequest(expected_active_count=-1)
