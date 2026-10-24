"""Shared test setup.

Page tests issue real HTTP requests through TestClient and hit the database.
When a PostgreSQL test service is available, this conftest creates the schema.
Without a reachable database those tests are skipped instead of failing, so the
pure-unit part of the suite still runs anywhere.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models import Base, engine


def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()


@pytest.fixture(scope="session", autouse=True)
def prepare_schema():
    if DB_AVAILABLE:
        Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def no_monitored_product_hosts(monkeypatch):
    """Keep description-link recognition independent of the shared test DB."""
    monkeypatch.setattr("app.asoc_description_safety._monitored_product_hosts", lambda: frozenset())


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_db: test requires a reachable PostgreSQL")


def pytest_collection_modifyitems(config, items):
    if DB_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="database not reachable; page tests run in CI with a postgres service")
    for item in items:
        if item.get_closest_marker("needs_db"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def synthetic_nested_product_namespace(monkeypatch):
    """Tests opt into a synthetic nested namespace; production has no default."""
    monkeypatch.setenv("PRODUCT_NESTED_NAMESPACES", "sandbox")


@pytest.fixture(autouse=True)
def synthetic_asoc_host_prefixes(monkeypatch):
    monkeypatch.setenv("ASOC_GITLAB_HOST_PREFIXES", '{"gitlab.primary.example.com":"primary","gitlab.enterprise.example.com":"enterprise"}')
