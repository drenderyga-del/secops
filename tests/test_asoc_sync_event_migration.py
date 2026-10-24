"""PostgreSQL upgrade contract for migrations/20260926_asoc_sync_event_confirmed.sql."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.models import engine


pytestmark = pytest.mark.needs_db

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "20260926_asoc_sync_event_confirmed.sql"


def test_upgrade_adds_confirmed_and_keeps_legacy_inserts_operator_confirmed():
    if engine.dialect.name != "postgresql":
        pytest.fail("the sync event migration contract requires PostgreSQL")

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Recreate the pre-migration table shape, then upgrade it twice.
            connection.execute(text("ALTER TABLE asoc_product_sync_events DROP COLUMN IF EXISTS confirmed"))
            connection.execute(
                text(
                    "INSERT INTO asoc_product_sync_events "
                    "(actor, previous_active_count, incoming_count, removed_count, registry_version) "
                    "VALUES ('operator', 4, 1, 3, :version)"
                ),
                {"version": "a" * 64},
            )
            for _run in range(2):
                connection.exec_driver_sql(MIGRATION.read_text(encoding="utf-8"))

            column = connection.execute(
                text(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'asoc_product_sync_events' AND column_name = 'confirmed'"
                )
            ).one()
            existing = connection.execute(
                text("SELECT confirmed FROM asoc_product_sync_events WHERE registry_version = :version"),
                {"version": "a" * 64},
            ).scalar_one()
            # A rolled-back release inserts without the column; its rows were
            # always operator-confirmed mass removals.
            legacy = connection.execute(
                text(
                    "INSERT INTO asoc_product_sync_events "
                    "(actor, previous_active_count, incoming_count, removed_count, registry_version) "
                    "VALUES ('operator', 4, 1, 3, :version) RETURNING confirmed"
                ),
                {"version": "b" * 64},
            ).scalar_one()
        finally:
            transaction.rollback()

    assert column.is_nullable == "NO"
    assert column.column_default == "true"
    assert existing is True
    assert legacy is True
