import threading
import time

import pytest

from app import integration_settings
from app.integration_settings import CredentialStoreUnavailable
from app.models import IntegrationConfig, IntegrationConfigEvent, SessionLocal


@pytest.mark.needs_db
def test_migration_row_lock_prevents_legacy_writer_from_restoring_old_ciphertext(
    monkeypatch,
):
    legacy_key = "legacy-bearer-key"
    dedicated_key = "dedicated-credential-key"
    monkeypatch.delenv("INTEGRATION_CREDENTIALS_KEY", raising=False)
    monkeypatch.setenv("PIPE_BEARER_TOKEN", legacy_key)

    with SessionLocal() as db:
        db.query(IntegrationConfigEvent).filter(
            IntegrationConfigEvent.connector == "asoc"
        ).delete()
        db.query(IntegrationConfig).filter(
            IntegrationConfig.connector == "asoc"
        ).delete()
        db.commit()
        integration_settings.save_connector(
            db,
            "asoc",
            {
                "url": "https://asoc.example/api",
                "token": "legacy-asoc-secret",
                "space_id": "space-1",
            },
            actor="legacy-operator",
        )

    migration_holds_lock = threading.Event()
    allow_migration = threading.Event()
    writer_started = threading.Event()
    original_try_decrypt = (
        integration_settings._try_decrypt_secrets_with_material
    )

    def pause_after_migration_lock(payload, material):
        if (
            threading.current_thread().name == "credential-migration"
            and material == dedicated_key
        ):
            migration_holds_lock.set()
            assert allow_migration.wait(timeout=5)
        return original_try_decrypt(payload, material)

    monkeypatch.setattr(
        integration_settings,
        "_try_decrypt_secrets_with_material",
        pause_after_migration_lock,
    )
    migration_result = {}
    writer_error = {}

    def run_migration():
        try:
            with SessionLocal() as db:
                migration_result["value"] = (
                    integration_settings.migrate_integration_credentials(
                        db,
                        dry_run=False,
                        dedicated_material=dedicated_key,
                        legacy_material=legacy_key,
                    )
                )
        except Exception as exc:  # pragma: no cover - assertion reports details
            migration_result["error"] = exc

    def run_legacy_writer():
        writer_started.set()
        try:
            with SessionLocal() as db:
                integration_settings.save_connector(
                    db,
                    "asoc",
                    {
                        "url": "https://asoc-new.example/api",
                        "token": "",
                        "space_id": "space-1",
                    },
                    actor="old-process",
                )
        except Exception as exc:
            writer_error["value"] = exc

    migration_thread = threading.Thread(
        target=run_migration,
        name="credential-migration",
    )
    writer_thread = threading.Thread(
        target=run_legacy_writer,
        name="legacy-writer",
    )
    try:
        migration_thread.start()
        assert migration_holds_lock.wait(timeout=5)
        writer_thread.start()
        assert writer_started.wait(timeout=5)
        time.sleep(0.2)
        assert writer_thread.is_alive(), "legacy writer did not wait for row lock"
    finally:
        allow_migration.set()
        migration_thread.join(timeout=5)
        writer_thread.join(timeout=5)

    assert "error" not in migration_result
    assert migration_result["value"].migrated == 1
    assert isinstance(writer_error.get("value"), CredentialStoreUnavailable)

    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", dedicated_key)
    monkeypatch.setenv("PIPE_BEARER_TOKEN", "rotated-ci-bearer")
    with SessionLocal() as db:
        runtime = integration_settings.load_runtime_connector("asoc", db)
        assert runtime.get("token") == "legacy-asoc-secret"
        assert runtime.get("url") == "https://asoc.example/api"
        db.query(IntegrationConfigEvent).filter(
            IntegrationConfigEvent.connector == "asoc"
        ).delete()
        db.query(IntegrationConfig).filter(
            IntegrationConfig.connector == "asoc"
        ).delete()
        db.commit()
