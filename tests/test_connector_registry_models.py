from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.connector_registry import Connector, ScannerInstance


def test_connector_and_instance_registry_enforces_stable_keys():
    engine = create_engine("sqlite:///:memory:")
    Connector.__table__.create(engine)
    ScannerInstance.__table__.create(engine)
    db = sessionmaker(bind=engine)()

    connector = Connector(key="gitlab", name="GitLab", connector_type="gitlab")
    db.add(connector)
    db.commit()
    db.add(
        ScannerInstance(
            connector_id=connector.id,
            instance_key="gitlab:primary",
            name="Primary GitLab",
            instance_type="scanner",
        )
    )
    db.commit()

    duplicate = ScannerInstance(
        connector_id=connector.id,
        instance_key="gitlab:primary",
        name="Duplicate",
        instance_type="scanner",
    )
    db.add(duplicate)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    else:  # pragma: no cover - documents the invariant for every supported DB
        raise AssertionError("instance_key must be unique")
