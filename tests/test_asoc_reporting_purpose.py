"""Operator-workbench ASOC projection used by the product group workbench."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.asoc_products import (
    MAPPING_MAPPED,
    MAPPING_MISSING_BI_ID,
    MAPPING_MISSING_TARGET,
    mapping_state,
    report_rows,
)
from app.models import AsocProduct, AsocProductRepositoryMapping, Product, Repository


@pytest.fixture(autouse=True)
def configured_gitlab_hosts(monkeypatch):
    monkeypatch.setenv("ASOC_PRODUCT_REFERENCE_HOSTS", "product.example.com")
    monkeypatch.setattr(
        "app.gitlab_url.known_gitlab_hosts",
        lambda: {"gitlab.example", "gitlab.primary.example.com", "gitlab.partner.example.com", "gitlab.demo.invalid"},
    )


def test_mapping_state_requires_bi_id_and_monitored_target():
    target = SimpleNamespace(id=7, name="ExampleApp")

    assert mapping_state("305", target) == MAPPING_MAPPED
    assert mapping_state("305", None) == MAPPING_MISSING_TARGET
    assert mapping_state(None, target) == MAPPING_MISSING_BI_ID
    assert mapping_state(None, None) == MAPPING_MISSING_BI_ID


def test_report_rows_classify_repository_projection_and_missing_target_mappings():
    engine = create_engine("sqlite://")
    for model in (AsocProduct, Product, Repository, AsocProductRepositoryMapping):
        model.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    by_id = Product(
        name="ExampleApp",
        path="example_app",
        gitlab_base_url="https://gitlab.example",
        asoc_product_id="asoc-305",
    )
    by_repository = Product(
        name="Identity",
        path="identity",
        gitlab_base_url="https://gitlab.example",
    )
    db.add_all([by_id, by_repository])
    db.flush()
    db.add_all(
        [
            Repository(product_id=by_id.id, path="example_app/api"),
            Repository(product_id=by_repository.id, path="identity/api"),
            AsocProduct(
                asoc_id="asoc-305",
                name="ExampleApp (BI_305)",
                title="ExampleApp",
                slug="example_app",
                bi_id="305",
                monitor_product_id=by_id.id,
                mapping_state="mapped",
            ),
            AsocProduct(
                asoc_id="asoc-404",
                name="Orphan (BI_404)",
                title="Orphan",
                slug="orphan",
                bi_id="404",
            ),
            AsocProduct(
                asoc_id="asoc-identity",
                name="Identity",
                title="Identity",
                slug="identity",
                bi_id=None,
                monitor_product_id=by_repository.id,
                mapping_state="mapped",
            ),
        ]
    )
    db.commit()

    rows = {row["slug"]: row for row in report_rows(db)}

    assert rows["example_app"]["mapping_state"] == MAPPING_MAPPED
    assert rows["example_app"]["monitor_product"].id == by_id.id
    assert rows["orphan"]["mapping_state"] == MAPPING_MISSING_TARGET
    assert rows["orphan"]["monitor_product"] is None
    assert rows["identity"]["mapping_state"] == MAPPING_MISSING_BI_ID
    assert rows["identity"]["monitor_product"].id == by_repository.id
    assert rows["identity"]["matched_by"] == "description_url"
