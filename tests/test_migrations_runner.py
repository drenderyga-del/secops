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
