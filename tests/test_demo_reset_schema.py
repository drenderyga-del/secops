"""Integration check for lifecycle triggers after destructive demo resets."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.models import engine


pytestmark = pytest.mark.needs_db


def test_reset_reapplies_asoc_lifecycle_migrations_and_seed_guards():
    if os.getenv("SCANOPS_REQUIRE_DEMO_SEED") != "1":
        pytest.skip("this check runs immediately after the disposable demo reset")
    if engine.dialect.name != "postgresql":
        pytest.fail("demo reset lifecycle checks require PostgreSQL")

    expected_triggers = {
        "trg_asoc_product_lifecycle_guard",
        "trg_require_asoc_product_lifecycle_write",
    }
    with engine.connect() as connection:
        trigger_names = set(
            connection.execute(
                text(
                    "SELECT trigger_row.tgname "
                    "FROM pg_trigger AS trigger_row "
                    "JOIN pg_class AS table_row ON table_row.oid = trigger_row.tgrelid "
                    "WHERE table_row.relname = 'asoc_products' "
                    "AND NOT trigger_row.tgisinternal"
                )
            ).scalars()
        )
        assert expected_triggers <= trigger_names

        migrations = set(
            connection.execute(
                text(
                    "SELECT name FROM schema_migrations "
                    "WHERE name IN ("
                    "'20260923_asoc_product_lifecycle.sql', "
                    "'20260925_asoc_product_safety.sql')"
                )
            ).scalars()
        )
        assert migrations == {
            "20260923_asoc_product_lifecycle.sql",
            "20260925_asoc_product_safety.sql",
        }

        product_count = connection.execute(
            text("SELECT count(*) FROM asoc_products")
        ).scalar_one()
        missing_guard_count = connection.execute(
            text(
                "SELECT count(*) FROM asoc_products AS product "
                "LEFT JOIN asoc_product_lifecycle_guards AS guard "
                "ON guard.asoc_id = product.asoc_id "
                "WHERE guard.asoc_id IS NULL"
            )
        ).scalar_one()

    assert product_count > 0, "the post-reset fixture should contain ASOC products"
    assert missing_guard_count == 0
