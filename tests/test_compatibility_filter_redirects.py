from urllib.parse import parse_qsl

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models import Base, Product
from app.routers.web import config_scan, scanners


@pytest.fixture()
def compatibility_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    product = Product(
        name="Compatibility target",
        path="quality/compatibility-target",
        gitlab_base_url="https://gitlab.example.test",
    )
    session.add(product)
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[config_scan.get_db] = override_db
    app.dependency_overrides[scanners.get_db] = override_db
    client = TestClient(app)
    try:
        yield client, product.id
    finally:
        client.close()
        app.dependency_overrides.pop(config_scan.get_db, None)
        app.dependency_overrides.pop(scanners.get_db, None)
        session.close()
        engine.dispose()


def test_classic_config_scan_product_filter_opens_the_live_product_page(
    compatibility_client,
):
    client, product_id = compatibility_client

    response = client.get(
        f"/classic/config-scan?product={product_id}&tab=history",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert response.url.path == f"/config-scan/products/{product_id}"
    assert parse_qsl(response.url.query.decode()) == [("tab", "history")]
    assert "Compatibility target" in response.text


@pytest.mark.parametrize("product", ["", "invalid", "0", "-3", "1&product=2"])
def test_invalid_classic_config_scan_product_filter_falls_back_without_lingering(
    compatibility_client,
    product,
):
    client, _product_id = compatibility_client

    response = client.get(
        f"/classic/config-scan?product={product}&tab=summary",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert response.url.path == "/config-scan/operator"
    assert parse_qsl(response.url.query.decode()) == [("tab", "summary")]
    assert "Compatibility target" in response.text


def test_classic_scanner_status_filter_is_not_preserved_as_false_ui_state(
    compatibility_client,
):
    client, _product_id = compatibility_client

    response = client.get(
        "/classic/scanners?status=offline&source=gitlab",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert response.url.path == "/scanners/operator"
    assert parse_qsl(response.url.query.decode()) == [("source", "gitlab")]
    assert "Scanner adapters" in response.text
