from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Product, Repository
from app.routers.web.signal_control import _filter_repositories_by_search


def _repository_db():
    engine = create_engine("sqlite://")
    Product.__table__.create(engine)
    Repository.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    product = Product(
        name="CBDC",
        path="fintech/cbdc",
        gitlab_base_url="https://gitlab.example",
    )
    db.add(product)
    db.flush()
    db.add_all(
        [
            Repository(product_id=product.id, path="fintech/cbdc/cbdc-cc"),
            Repository(product_id=product.id, path="fintech/cbdc/cbdc-access"),
        ]
    )
    db.commit()
    return db


def _search(db, query: str) -> list[str]:
    filtered = _filter_repositories_by_search(
        db.query(Repository).outerjoin(Product, Repository.product_id == Product.id),
        query,
    )
    return [row.path for row in filtered.order_by(Repository.path).all()]


def test_repository_search_splits_spaces_hyphens_and_paths():
    db = _repository_db()
    try:
        assert _search(db, "CBDC CC") == ["fintech/cbdc/cbdc-cc"]
        assert _search(db, "Fintech CBDC") == [
            "fintech/cbdc/cbdc-access",
            "fintech/cbdc/cbdc-cc",
        ]
    finally:
        db.close()


def test_repository_search_preserves_partial_names_and_literal_punctuation():
    db = _repository_db()
    try:
        product = Product(name="R&D", path="sandbox/research", gitlab_base_url="https://gitlab.example")
        db.add(product)
        db.flush()
        db.add(Repository(product_id=product.id, path="sandbox/research/gatewayclient"))
        db.commit()
        expected = ["sandbox/research/gatewayclient"]
        assert _search(db, "gateway") == expected
        assert _search(db, "R&D") == expected
        assert _search(db, "%") == []
        assert _search(db, "_") == []
    finally:
        db.close()
