from pathlib import Path

import pytest
import yaml

from scripts import seed_demo_data


ROOT = Path(__file__).resolve().parents[1]


def test_demo_seed_creates_ptai_scan_rows():
    source = (ROOT / "scripts/seed_demo_data.py").read_text()
    assert "PtaiScan" in source
    assert "def create_ptai_scans" in source
    for status in ["PENDING", "IN_PROGRESS", "SUCCESS", "ERROR", "PARTIAL_SUCCESS", "TRIGGER_FAILED"]:
        assert status in source
    for stage in ["Queued", "Scan", "Done", "Failed"]:
        assert stage in source


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgresql://postgres:test@localhost:5432/scanops_demo", "scanops_demo"),
        ("postgresql://postgres:test@127.0.0.1:5432/scanops_demo", "scanops_demo"),
        ("postgresql://postgres:test@[::1]:5432/scanops_demo", "scanops_demo"),
        ("postgresql://postgres:test@postgres:5432/scanops_demo", "scanops_demo"),
        ("postgresql://postgres:test@localhost:5432/ui_refresh_qa", "ui_refresh_qa"),
    ],
)
def test_demo_database_guard_accepts_only_explicit_demo_names(url, expected):
    assert seed_demo_data._assert_demo_database(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres:test@localhost:5432/security_dashboard",
        "postgresql://postgres:test@localhost:5432/production",
        "postgresql://postgres:secret@db.corp.example:5432/scanops_demo",
        "postgresql://postgres:secret@postgres.evil.example:5432/ui_refresh_qa",
        "postgresql://postgres:secret@127.0.0.1.evil.example:5432/scanops_demo",
        "postgresql://postgres:secret@localhost:5432/scanops_demo?host=db.corp.example",
        "https://localhost:5432/scanops_demo",
        "",
    ],
)
def test_demo_database_guard_fails_closed(url):
    with pytest.raises(RuntimeError, match="Refusing") as error:
        seed_demo_data._assert_demo_database(url)
    assert "secret" not in str(error.value)
    assert "db.corp.example" not in str(error.value)


def test_profiles_cross_expected_volume_boundaries():
    kitchen = seed_demo_data.PLANS["kitchen-sink"]
    volume = seed_demo_data.PLANS["volume"]
    assert kitchen.pipeline_count < 200
    assert kitchen.ptai_count < 200
    assert volume.pipeline_count > 200
    assert volume.ptai_count > 200
    assert volume.product_count * volume.repositories_per_product > 200


def test_anchor_and_demo_secret_payload_are_deterministic_contracts(monkeypatch):
    anchor = seed_demo_data._parse_anchor("2026-07-22T12:00:00Z")
    assert anchor.isoformat() == "2026-07-22T12:00:00+00:00"
    assert seed_demo_data.parse_args([]).anchor == seed_demo_data.DEFAULT_ANCHOR

    encrypted = seed_demo_data._encrypted_demo_secrets("fixture-key", {"token": "demo-token"})
    # Fernet is randomized, but the payload must be decryptable by the actual
    # application code, not merely by a duplicate test implementation.
    from app.integration_settings import _decrypt_secrets

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", "fixture-key")
    assert _decrypt_secrets(encrypted) == {"token": "demo-token"}


def test_now_anchor_is_utc_and_rounded_to_the_minute(monkeypatch):
    class FrozenDateTime(seed_demo_data.datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == seed_demo_data.timezone.utc
            return cls(2031, 4, 5, 6, 7, 48, 913_000, tzinfo=tz)

    monkeypatch.setattr(seed_demo_data, "datetime", FrozenDateTime)
    anchor = seed_demo_data._parse_anchor(" now ")
    assert anchor.isoformat() == "2031-04-05T06:07:00+00:00"


def test_timeline_start_keeps_the_latest_lifecycle_event_at_the_anchor():
    anchor = seed_demo_data._parse_anchor("2031-04-05T06:07:00Z")
    start = seed_demo_data._timeline_start(
        anchor,
        slot_minutes=13,
        max_event_offset_minutes=40,
    )

    assert start + seed_demo_data.timedelta(minutes=40) < anchor


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("requested", ()),
        ("queued", ()),
        ("running", (("trigger", "succeeded"), ("scan", "running"))),
        ("cancel_requested", (("trigger", "succeeded"), ("scan", "running"))),
        (
            "delivering",
            (
                ("trigger", "succeeded"),
                ("scan", "succeeded"),
                ("normalize", "succeeded"),
                ("deliver", "running"),
            ),
        ),
        (
            "succeeded",
            (
                ("trigger", "succeeded"),
                ("scan", "succeeded"),
                ("normalize", "succeeded"),
                ("deliver", "succeeded"),
            ),
        ),
        (
            "partial",
            (
                ("trigger", "succeeded"),
                ("scan", "succeeded"),
                ("normalize", "succeeded"),
                ("deliver", "failed"),
            ),
        ),
        ("failed", (("trigger", "succeeded"), ("scan", "failed"))),
        ("cancelled", (("trigger", "succeeded"), ("scan", "cancelled"))),
    ],
)
def test_execution_steps_match_the_seeded_lifecycle(status, expected):
    assert seed_demo_data._execution_step_states(status) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("requested", False),
        ("queued", False),
        ("running", False),
        ("delivering", True),
        ("cancel_requested", False),
        ("succeeded", True),
        ("partial", True),
        ("failed", False),
        ("cancelled", False),
    ],
)
def test_execution_evidence_requires_a_normalized_lifecycle(status, expected):
    assert seed_demo_data._execution_produces_artifact(status) is expected


@pytest.mark.parametrize(
    ("status", "index", "expected"),
    [
        ("requested", 0, None),
        ("running", 2, None),
        ("delivering", 3, "running"),
        ("succeeded", 5, "confirmed"),
        ("partial", 6, "failed"),
        ("partial", 15, "skipped"),
        ("failed", 7, None),
        ("cancelled", 8, None),
    ],
)
def test_execution_delivery_matches_the_seeded_lifecycle(status, index, expected):
    assert seed_demo_data._execution_delivery_status(status, index) == expected


def test_disposable_compose_resets_a_fresh_volume_profile_at_current_time():
    compose = yaml.safe_load((ROOT / "docker-compose.demo.yml").read_text(encoding="utf-8"))
    assert compose["services"]["seed"]["command"] == [
        "python",
        "scripts/seed_demo_data.py",
        "--profile",
        "volume",
        "--reset",
        "--anchor",
        "now",
    ]


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled"])
def test_config_scan_counts_match_lifecycle(status):
    total = 28
    scanned, findings = seed_demo_data._config_run_counts(status, total, 3)
    assert 0 <= findings <= scanned <= total
    if status == "queued":
        assert (scanned, findings) == (0, 0)
    elif status == "running":
        assert 0 < scanned < total
    elif status == "succeeded":
        assert scanned == total


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled"])
def test_iac_run_metrics_are_derived_from_repository_rows(status):
    repository_count = 224 if status == "queued" else 16
    states = [
        seed_demo_data._iac_repository_state(status, index, repository_count)
        for index in range(repository_count)
    ]
    metrics = seed_demo_data._iac_run_metrics(states)

    assert metrics["total_repositories"] == repository_count
    assert metrics["candidate_repositories"] == sum(1 for row in states if row["has_iac"])
    assert metrics["scanned_repositories"] == sum(row["scan_status"] == "succeeded" for row in states)
    assert metrics["uploaded_repositories"] == sum(row["upload_status"] == "uploaded" for row in states)
    assert metrics["findings_count"] == sum(row["findings_count"] for row in states)
    assert metrics["findings_count"] == sum(
        metrics[f"{severity}_count"] for severity in ("critical", "high", "medium", "low", "info")
    )
    assert all(
        row["findings_count"]
        == sum(row[f"{severity}_count"] for severity in ("critical", "high", "medium", "low", "info"))
        for row in states
    )
    if status == "queued":
        assert metrics["total_repositories"] > 200
        assert metrics["scanned_repositories"] == 0
        assert metrics["findings_count"] == 0


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("http://qa_mock:18080/", "http://qa_mock:18080"),
        ("http://localhost:18090", "http://localhost:18090"),
        ("http://127.0.0.1:18090", "http://127.0.0.1:18090"),
    ],
)
def test_mock_base_url_accepts_only_explicit_local_hosts(value, expected):
    assert seed_demo_data._normalize_mock_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://qa_mock:18080",
        "http://example.com:8090",
        "http://qa_mock:18080/path",
        "http://user:pass@localhost:18090",
        "http://localhost:18090?token=secret",
    ],
)
def test_mock_base_url_rejects_nonlocal_or_ambiguous_targets(value):
    with pytest.raises(ValueError, match="mock-base-url"):
        seed_demo_data._normalize_mock_base_url(value)


def test_seed_uses_task326_public_local_fixture_credentials():
    source = (ROOT / "scripts/seed_demo_data.py").read_text(encoding="utf-8")
    for value in (
        "local-gitlab-token",
        "local-asoc-token",
        "local-ptai-token",
        "local-mattermost-token",
    ):
        assert value in source


def test_seed_exercises_independent_gitlab_instances_and_health_states():
    source = (ROOT / "scripts/seed_demo_data.py").read_text(encoding="utf-8")

    assert '"gl-1111111111111111"' in source
    assert '"gl-2222222222222222"' in source
    assert '"instances_json"' in source
    assert "IntegrationInstanceTestState" in source
    assert '"polling": {"enabled": True}' in source
    assert '"polling": {"enabled": False}' in source
    assert 'test_status="healthy"' in source
    assert 'test_status="failed"' in source
    assert '"integration_instance_test_states"' in source
    assert '"Disabled for recovery walkthrough"' in source
    assert '{"webhook_url": mattermost_url},\n            False,' in source


def test_seed_uses_canonical_asoc_delivery_destination():
    source = (ROOT / "scripts/seed_demo_data.py").read_text(encoding="utf-8")

    assert 'destination="asoc:demo"' in source
    assert 'destination="asoc-demo"' not in source
