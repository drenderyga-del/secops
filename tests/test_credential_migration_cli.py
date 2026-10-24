import os

import pytest

from app.integration_settings import IntegrationConfigError
from scripts.migrate_integration_credentials import (
    _database_target,
    _load_reset_manifest,
    _write_reset_manifest,
)


def test_database_target_is_explicit_and_redacts_credentials():
    assert (
        _database_target(
            "postgresql://operator:secret@postgres:5433/security_dashboard"
        )
        == "postgres:5433/security_dashboard"
    )

    with pytest.raises(IntegrationConfigError, match="explicitly set"):
        _database_target("")

    with pytest.raises(IntegrationConfigError, match="query parameters"):
        _database_target(
            "postgresql://shown:5432/shown_db"
            "?host=actual&port=6543&dbname=actual_db"
        )

    with pytest.raises(IntegrationConfigError, match="query parameters"):
        _database_target(
            "postgresql://postgres:5432/security_dashboard"
            "?options=-csearch_path%3Dother_schema"
        )


def test_reset_manifest_is_mode_600_and_bound_to_database(tmp_path):
    path = tmp_path / "reset.json"
    entries = {"asoc": "a" * 64}

    _write_reset_manifest(
        path,
        database_target="postgres:5432/security_dashboard",
        entries=entries,
    )

    assert os.stat(path).st_mode & 0o777 == 0o600
    assert (
        _load_reset_manifest(
            path,
            database_target="postgres:5432/security_dashboard",
        )
        == entries
    )
    with pytest.raises(IntegrationConfigError, match="does not match"):
        _load_reset_manifest(
            path,
            database_target="postgres:5432/another_database",
        )


def test_reset_manifest_refuses_broad_permissions(tmp_path):
    path = tmp_path / "reset.json"
    _write_reset_manifest(
        path,
        database_target="postgres:5432/security_dashboard",
        entries={"asoc": "a" * 64},
    )
    path.chmod(0o644)

    with pytest.raises(IntegrationConfigError, match="mode 600"):
        _load_reset_manifest(
            path,
            database_target="postgres:5432/security_dashboard",
        )


def test_reset_manifest_reports_unwritable_destination(monkeypatch, tmp_path):
    def deny_open(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "open", deny_open)

    with pytest.raises(IntegrationConfigError, match="cannot be created"):
        _write_reset_manifest(
            tmp_path / "reset.json",
            database_target="postgres:5432/security_dashboard",
            entries={"asoc": "a" * 64},
        )
