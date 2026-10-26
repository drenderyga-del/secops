import threading
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.asoc_products import sync_asoc_products
from app.main import _setup_database
from app.models import AsocProduct, AsocProductLifecycleGuard, SessionLocal, engine
from app.migrations_runner import schema_bootstrap_lock


@pytest.mark.needs_db
def test_schema_bootstrap_lock_serializes_web_and_worker_startup():
    second_entered = threading.Event()
    second_finished = threading.Event()

    def _second_bootstrap():
        with schema_bootstrap_lock(engine):
            second_entered.set()
        second_finished.set()

    with schema_bootstrap_lock(engine):
        worker = threading.Thread(target=_second_bootstrap, daemon=True)
        worker.start()
        assert not second_entered.wait(timeout=0.2)

    assert second_entered.wait(timeout=5)
    assert second_finished.wait(timeout=5)
    worker.join(timeout=5)
    assert not worker.is_alive()


@pytest.mark.needs_db
def test_asoc_lifecycle_migration_allows_current_sync_and_blocks_legacy_writes():
    _setup_database()
    asoc_id = f"lifecycle-migration-test-{uuid4().hex}"
    db = SessionLocal()
    try:
        result = sync_asoc_products(
            db,
            [{"id": asoc_id, "name": "Lifecycle migration test"}],
        )
        assert result["created"] == 1
        assert db.query(AsocProductLifecycleGuard).filter_by(asoc_id=asoc_id).one()

        with pytest.raises(DBAPIError):
            db.execute(
                text("UPDATE asoc_products SET description = :description WHERE asoc_id = :asoc_id"),
                {"description": "raw legacy Description", "asoc_id": asoc_id},
            )
            db.commit()
        db.rollback()
    finally:
        db.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM asoc_product_lifecycle_guards WHERE asoc_id = :asoc_id"),
                {"asoc_id": asoc_id},
            )
            connection.execute(
                text("DELETE FROM asoc_products WHERE asoc_id = :asoc_id"),
                {"asoc_id": asoc_id},
            )


@pytest.mark.needs_db
def test_lifecycle_upgrade_exclusive_lock_has_bounded_wait():
    from pathlib import Path
    if engine.dialect.name != 'postgresql':
        pytest.skip('table lock timeout requires PostgreSQL')
    migration = (Path(__file__).resolve().parents[1] / 'migrations/20260925_asoc_product_safety.sql').read_text()
    timeout = "SET LOCAL lock_timeout = '15s';"
    assert timeout in migration
    assert migration.index(timeout) < migration.index('LOCK TABLE asoc_products')
    # Preserve the production policy while making contention acceptance fast.
    statement = migration.replace(timeout, "SET LOCAL lock_timeout = '150ms';")
    with engine.begin() as blocker:
        blocker.exec_driver_sql('LOCK TABLE asoc_products IN ACCESS SHARE MODE')
        with engine.connect() as contender:
            transaction = contender.begin()
            try:
                with pytest.raises(DBAPIError) as error:
                    contender.exec_driver_sql(statement)
                assert getattr(error.value.orig, 'pgcode', None) == '55P03'
            finally:
                transaction.rollback()
