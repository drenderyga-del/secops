"""Tests for the scanner-neutral Product / Repository / SecurityControl layer."""
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.control_coverage import BINDING_STATUSES, coverage_by_repository
from app.models import Product, Repository, RepositoryControlBinding, SecurityControl, engine


def _session():
    engine = create_engine("sqlite://")
    for model in (Product, Repository, SecurityControl, RepositoryControlBinding):
        model.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_expected_vs_configured_coverage_uses_control_bindings_not_scanner_runs():
    db = _session()
    try:
        product = Product(name="example_app", path="sandbox/example_app", gitlab_base_url="https://gitlab.example")
        db.add(product)
        db.flush()
        repository = Repository(product_id=product.id, path="sandbox/example_app/api")
        db.add(repository)
        db.flush()

        sast = SecurityControl(control_key="sast", name="SAST", category="code")
        iac = SecurityControl(control_key="iac", name="IaC", category="infrastructure")
        optional = SecurityControl(control_key="dast", name="DAST", category="runtime")
        db.add_all([sast, iac, optional])
        db.flush()
        db.add_all([
            RepositoryControlBinding(repository_id=repository.id, security_control_id=sast.id, status="configured"),
            RepositoryControlBinding(repository_id=repository.id, security_control_id=iac.id, status="missing"),
            RepositoryControlBinding(repository_id=repository.id, security_control_id=optional.id, status="excluded"),
        ])
        db.commit()

        coverage = coverage_by_repository(db, [repository.id])[repository.id]

        assert coverage == {"expected": 2, "configured": 1, "missing": 1, "excluded": 1}
    finally:
        db.close()


def test_control_coverage_keeps_unknown_as_expected_but_not_configured():
    db = _session()
    try:
        product = Product(name="example_app", path="sandbox/example_app", gitlab_base_url="https://gitlab.example")
        db.add(product)
        db.flush()
        repository = Repository(product_id=product.id, path="sandbox/example_app/web")
        control = SecurityControl(control_key="dependency-scan", name="Dependency scan")
        db.add_all([repository, control])
        db.flush()
        db.add(RepositoryControlBinding(repository_id=repository.id, security_control_id=control.id, status="unknown"))
        db.commit()

        assert coverage_by_repository(db, [repository.id])[repository.id] == {
            "expected": 1,
            "configured": 0,
            "missing": 1,
            "excluded": 0,
        }
        assert BINDING_STATUSES == {"configured", "missing", "unknown", "excluded"}
    finally:
        db.close()


def test_inactive_controls_do_not_count_toward_expected_coverage():
    db = _session()
    try:
        product = Product(name="example_app", path="sandbox/example_app", gitlab_base_url="https://gitlab.example")
        db.add(product)
        db.flush()
        repository = Repository(product_id=product.id, path="sandbox/example_app/web")
        active = SecurityControl(control_key="sast", name="SAST", is_active=True)
        retired = SecurityControl(control_key="legacy-dast", name="Legacy DAST", is_active=False)
        db.add_all([repository, active, retired])
        db.flush()
        db.add_all([
            RepositoryControlBinding(repository_id=repository.id, security_control_id=active.id, status="configured"),
            RepositoryControlBinding(repository_id=repository.id, security_control_id=retired.id, status="missing"),
        ])
        db.commit()

        assert coverage_by_repository(db, [repository.id])[repository.id] == {
            "expected": 1,
            "configured": 1,
            "missing": 0,
            "excluded": 0,
        }
    finally:
        db.close()


def test_binding_model_enforces_the_same_status_contract_on_fresh_schema():
    checks = [constraint for constraint in RepositoryControlBinding.__table__.constraints if isinstance(constraint, CheckConstraint)]
    assert len(checks) == 1
    assert "configured" in str(checks[0].sqltext)
    assert "excluded" in str(checks[0].sqltext)
    db = _session()
    try:
        product = Product(name="example_app", path="sandbox/example_app", gitlab_base_url="https://gitlab.example")
        db.add(product)
        db.flush()
        repository = Repository(product_id=product.id, path="sandbox/example_app/web")
        control = SecurityControl(control_key="sast", name="SAST")
        db.add_all([repository, control])
        db.flush()
        db.add(RepositoryControlBinding(
            repository_id=repository.id,
            security_control_id=control.id,
            status="not-a-valid-status",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.close()


@pytest.mark.needs_db
def test_migration_repairs_status_constraint_on_a_legacy_postgres_table():
    schema = f"control_migration_{uuid4().hex}"
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "20260720_security_controls.sql").read_text(encoding="utf-8")
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            conn.execute(text("CREATE TABLE repositories (id INTEGER PRIMARY KEY)"))
            conn.execute(text("""
                CREATE TABLE security_controls (
                    id INTEGER PRIMARY KEY,
                    control_key VARCHAR(128) NOT NULL UNIQUE,
                    name VARCHAR(255) NOT NULL,
                    category VARCHAR(64),
                    description TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ
                )
            """))
            conn.execute(text("""
                CREATE TABLE repository_control_bindings (
                    id INTEGER PRIMARY KEY,
                    repository_id INTEGER NOT NULL REFERENCES repositories(id),
                    security_control_id INTEGER NOT NULL REFERENCES security_controls(id),
                    scope VARCHAR(255) NOT NULL DEFAULT '*',
                    status VARCHAR(32) NOT NULL DEFAULT 'missing'
                )
            """))
            conn.execute(text(sql))
            constraint_exists = conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_repository_control_status'
                      AND conrelid = 'repository_control_bindings'::regclass
                )
            """)).scalar_one()
            assert constraint_exists
            conn.execute(text("INSERT INTO repositories (id) VALUES (1)"))
            conn.execute(text("INSERT INTO security_controls (id, control_key, name) VALUES (1, 'sast', 'SAST')"))
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                conn.execute(text("""
                    INSERT INTO repository_control_bindings
                        (id, repository_id, security_control_id, status)
                    VALUES (1, 1, 1, 'not-a-valid-status')
                """))
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_products_surface_and_api_keep_control_coverage_visible():
    with open("app/templates/product_repositories.html", encoding="utf-8") as template:
        assert "Control coverage" in template.read()
    with open("app/routers/api.py", encoding="utf-8") as route:
        source = route.read()
    assert '/repositories/{repository_id}/controls' in source
    assert '/security-controls' in source
