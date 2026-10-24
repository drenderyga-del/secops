from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.integration_settings import connector_snapshot, record_test_result
from app.models import (
    IntegrationConfig,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
)
from app.routers.web.scanners import (
    _combined_connection_contract,
    _connection_contract,
    _latest_contract,
    _ptai_connection_contract,
    _ready_connection_count,
)


def _snapshot(**overrides):
    snapshot = {
        "enabled": True,
        "configured": True,
        "error": None,
        "test_status": None,
        "test_message": None,
    }
    snapshot.update(overrides)
    return snapshot


def _integration_session():
    engine = create_engine("sqlite:///:memory:")
    IntegrationConfig.__table__.create(engine)
    IntegrationConfigEvent.__table__.create(engine)
    IntegrationInstanceTestState.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_connection_contract_explains_configuration_source_without_values():
    contract = _connection_contract(
        _snapshot(
            configured=False,
            fields=[{"name": "token", "value": "must-not-render"}],
        ),
        "GitLab",
    )

    assert contract == {
        "state": "degraded",
        "label": "Setup required",
        "reason": "Source: GitLab is not configured in Integrations.",
    }
    assert "must-not-render" not in str(contract)


def test_connection_test_failure_is_distinct_from_runtime_execution_state():
    contract = _connection_contract(
        _snapshot(
            test_status="failed",
            test_message="Connection test failed: endpoint is unreachable",
        ),
        "ASOC",
    )

    assert contract["state"] == "degraded"
    assert contract["label"] == "Test failed"
    assert contract["reason"].startswith("Source: ASOC")


def test_configured_untested_connector_is_unknown_not_connected():
    contract = _connection_contract(_snapshot(), "GitLab")

    assert contract == {
        "state": "unknown",
        "label": "Configured · untested",
        "reason": (
            "Source: GitLab is configured; no connection test is recorded."
        ),
    }


def test_combined_connection_names_the_degraded_source():
    healthy = _connection_contract(
        _snapshot(test_status="healthy"),
        "GitLab",
    )
    degraded = _connection_contract(
        _snapshot(configured=False),
        "ASOC",
    )

    contract = _combined_connection_contract(
        [("GitLab", healthy), ("ASOC", degraded)]
    )

    assert contract["state"] == "degraded"
    assert contract["label"] == "ASOC needs attention"
    assert "ASOC is not configured in Integrations" in contract["reason"]


def test_untested_connectors_do_not_inflate_adapter_connected_count():
    gitlab = _connection_contract(_snapshot(), "GitLab")
    asoc = _connection_contract(_snapshot(), "ASOC")
    ptai = _connection_contract(_snapshot(), "PT AI")
    scanners = [
        {"connection": gitlab},
        {"connection": gitlab},
        {
            "connection": _combined_connection_contract(
                [("GitLab", gitlab), ("ASOC", asoc)]
            )
        },
        {"connection": _ptai_connection_contract(asoc, ptai)},
    ]

    assert {scanner["connection"]["state"] for scanner in scanners} == {"unknown"}
    assert _ready_connection_count(scanners) == 0


def test_environment_test_transitions_update_scanner_fleet_connected_aggregate(
    monkeypatch,
):
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.env.example/api/v4")
    monkeypatch.setenv("GITLAB_PAT", "scanner-fleet-environment-secret")
    monkeypatch.delenv("GITLAB_INSTANCES", raising=False)
    db = _integration_session()

    def adapter_count():
        connection = _connection_contract(
            connector_snapshot(db, "gitlab"),
            "GitLab",
        )
        return _ready_connection_count(
            [
                {"connection": connection},
                {"connection": connection},
            ]
        )

    assert adapter_count() == 0

    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Connected · authentication and project discovery verified",
    )
    assert adapter_count() == 2

    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=False,
        message="Connection test failed: endpoint is unreachable",
    )
    assert adapter_count() == 0

    record_test_result(
        db,
        "gitlab",
        actor="operator",
        success=True,
        message="Connected · recovered",
    )
    assert adapter_count() == 2
    assert db.query(IntegrationConfig).count() == 0


def test_ptai_telemetry_can_be_absent_when_required_asoc_is_ready():
    asoc = _connection_contract(_snapshot(test_status="healthy"), "ASOC")
    ptai = _connection_contract(_snapshot(configured=False), "PT AI")

    contract = _ptai_connection_contract(asoc, ptai)

    assert contract["state"] == "healthy"
    assert contract["label"] == "ASOC ready"
    assert "optional" in contract["reason"]


def test_latest_execution_empty_state_names_the_missing_source():
    contract = _latest_contract(
        None,
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        empty_reason="Source: Config Guard has no recorded scan run.",
    )

    assert contract == {
        "state": "idle",
        "label": "No run recorded",
        "occurred_at": None,
        "summary": "Source: Config Guard has no recorded scan run.",
    }
