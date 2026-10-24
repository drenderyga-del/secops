"""PostgreSQL contract for migrations/20260926_asoc_lifecycle_unlink_and_legacy_links.sql."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models import engine


pytestmark = pytest.mark.needs_db

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _apply(connection, name: str) -> None:
    connection.exec_driver_sql((MIGRATIONS / name).read_text(encoding="utf-8"))


def test_deleting_a_linked_product_unlinks_cards_but_other_writes_stay_gated():
    if engine.dialect.name != "postgresql":
        pytest.fail("the lifecycle guard contract requires PostgreSQL")

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            _apply(connection, "20260925_asoc_product_safety.sql")
            connection.execute(text("ALTER TABLE asoc_products DROP COLUMN IF EXISTS connected_before_tracking"))
            product_id = connection.execute(
                text(
                    "INSERT INTO products (name, path, gitlab_base_url) "
                    "VALUES ('qa-guard', 'qa/guard', 'https://gitlab.example') RETURNING id"
                )
            ).scalar_one()
            connection.execute(text("SELECT set_config('scanops.asoc_product_lifecycle_write', 'allowed', true)"))
            connection.execute(
                text(
                    "INSERT INTO asoc_products (asoc_id, name, mapping_state, monitor_product_id, is_active, "
                    "repositories_count, matched_repositories_count) "
                    "VALUES ('qa-guard-card', 'QA guard', 'mapped', :product_id, true, 0, 0)"
                ),
                {"product_id": product_id},
            )
            # Upgrade: backfill must mark the undated linked card.
            _apply(connection, "20260926_asoc_lifecycle_unlink_and_legacy_links.sql")
            legacy = connection.execute(
                text("SELECT connected_before_tracking FROM asoc_products WHERE asoc_id = 'qa-guard-card'")
            ).scalar_one()

            # Leave the sync marker behind: from here on, writes are not a sync.
            connection.execute(text("SELECT set_config('scanops.asoc_product_lifecycle_write', '', true)"))
            connection.execute(text("DELETE FROM products WHERE id = :product_id"), {"product_id": product_id})
            unlinked = connection.execute(
                text("SELECT monitor_product_id FROM asoc_products WHERE asoc_id = 'qa-guard-card'")
            ).scalar_one()

            savepoint = connection.begin_nested()
            with pytest.raises(DBAPIError, match="lifecycle-aware ScanOps release"):
                connection.execute(text("UPDATE asoc_products SET name = 'tampered' WHERE asoc_id = 'qa-guard-card'"))
            savepoint.rollback()
        finally:
            transaction.rollback()

    assert legacy is True
    assert unlinked is None
