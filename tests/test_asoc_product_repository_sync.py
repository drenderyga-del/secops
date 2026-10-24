from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.asoc_products import (
    ASOC_MATCH_CONFLICT,
    ASOC_MATCH_MAPPED,
    ASOC_MATCH_PARTIAL,
    ASOC_SYNC_STATE_KEY,
    AsocSyncConfirmationRequiredError,
    AsocSyncInProgressError,
    _ASOC_SYNC_LOCK,
    asoc_sync_metadata,
    asoc_rows_by_product,
    asoc_product_url,
    fetch_and_sync,
    parse_bi_id,
    product_scope,
    read_sync_outcome,
    record_sync_outcome,
    report_rows,
    run_scheduled_sync,
    sync_asoc_products,
)
from app.models import (
    ActivePipeline,
    AppState,
    AsocProduct,
    AsocProductLifecycleGuard,
    AsocProductSyncEvent,
    AsocProductRepositoryMapping,
    Product,
    Repository,
)
from app.models.database import Base
from app.models.pipeline import PipelineStatus
from app.product_registry import RegistryQuery, build_registry


@pytest.fixture(autouse=True)
def configured_gitlab_hosts(monkeypatch):
    monkeypatch.setenv("ASOC_PRODUCT_REFERENCE_HOSTS", "product.example.com")
    monkeypatch.setattr(
        "app.gitlab_url.known_gitlab_hosts",
        lambda: {"gitlab.example", "gitlab.primary.example.com", "gitlab.partner.example.com", "gitlab.demo.invalid"},
    )


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.mark.parametrize(
    ("name", "expected_id", "expected_title"),
    [
        ("Example portfolio (BI_11/BI_22)", "11/22", "Example portfolio"),
        ("ExampleApp (BI_000141/BI-2170)", "141/2170", "ExampleApp"),
        ("Example widgets (BI_141)", "141", "Example widgets"),
    ],
)
def test_asoc_name_parses_single_and_composite_bi_ids(name, expected_id, expected_title):
    assert parse_bi_id(name) == (expected_id, expected_title)


def test_product_mapping_uses_description_links_not_asoc_name_or_slug():
    db = _db()
    product = Product(
        name="Example example_app service",
        path="sandbox/sample-orders",
        gitlab_base_url="https://gitlab.example",
    )
    db.add(product)
    db.flush()
    db.add_all(
        [
            Repository(product_id=product.id, path="sandbox/sample-orders/api"),
            Repository(product_id=product.id, path="sandbox/sample-orders/web"),
        ]
    )
    db.commit()

    result = sync_asoc_products(
        db,
        [
            {
                "id": "asoc-product-1",
                "name": "Демонстрационный сервис платежей (BI_77)",
                "slug": "unrelated-technical-slug",
                "description": (
                    "https://gitlab.example/sandbox/sample-orders\n"
                    "https://product.example.com/product/BI_77"
                ),
            }
        ],
    )

    card = db.query(AsocProduct).one()
    card.description = f"{card.description or ''}\nScanOps-Scope: internal"
    db.commit()
    db.refresh(product)
    assert result[ASOC_MATCH_MAPPED] == 1
    assert card.mapping_state == ASOC_MATCH_MAPPED
    assert card.monitor_product_id == product.id
    assert card.repositories_count == 1
    assert card.matched_repositories_count == 1
    projection = report_rows(db)[0]
    assert projection["scope"] == "internal"
    assert projection["description_links"]
    product_360_cards = asoc_rows_by_product(report_rows(db))[product.id]
    assert product_360_cards[0]["scope"] == "internal"
    assert product_360_cards[0]["description_links"] == projection["description_links"]
    assert product.asoc_product_id == "asoc-product-1"
    assert product.asoc_product_slug == "unrelated-technical-slug"


def test_product_mapping_allows_one_asoc_product_to_span_multiple_gitlab_groups():
    db = _db()
    example_app = Product(
        name="example_app",
        path="sandbox/example_app",
        gitlab_base_url="https://gitlab.example",
    )
    identity = Product(
        name="identity",
        path="sandbox/identity",
        gitlab_base_url="https://gitlab.example",
    )
    partial_target = Product(
        name="partial-target",
        path="sandbox/partial",
        gitlab_base_url="https://gitlab.example",
    )
    db.add_all([example_app, identity, partial_target])
    db.flush()
    db.add_all(
        [
            Repository(product_id=example_app.id, path="sandbox/example_app/api"),
            Repository(product_id=identity.id, path="sandbox/identity/api"),
            Repository(product_id=partial_target.id, path="sandbox/partial/api"),
        ]
    )
    db.commit()

    sync_asoc_products(
        db,
        [
            {
                "id": "partial",
                "name": "ASOC example_app card (BI_1)",
                "slug": "not-the-scanops-name",
                "description": (
                    "https://gitlab.example/sandbox/partial/api\n"
                    "https://gitlab.example/sandbox/partial/missing"
                ),
            },
            {
                "id": "multi-group",
                "name": "ASOC example_app card (BI_2)",
                "description": (
                    "https://gitlab.example/sandbox/example_app/api\n"
                    "https://gitlab.example/sandbox/identity/api"
                ),
            },
        ],
    )

    cards = {card.asoc_id: card for card in db.query(AsocProduct).all()}
    assert cards["partial"].mapping_state == ASOC_MATCH_PARTIAL
    assert cards["partial"].monitor_product_id == partial_target.id
    assert cards["multi-group"].mapping_state == ASOC_MATCH_MAPPED
    assert cards["multi-group"].monitor_product_id == example_app.id
    links = db.query(AsocProductRepositoryMapping).filter_by(asoc_id="multi-group").all()
    assert {link.product_id for link in links} == {example_app.id, identity.id}
    assert cards["multi-group"].first_connected_at is not None
    registry = build_registry(db, RegistryQuery())
    multi_group_row = next(
        row for row in registry["rows"] if row["asoc_id"] == "multi-group"
    )
    assert len(multi_group_row["groups"]) == 2
    assert "description" not in multi_group_row
    assert registry["summary"]["total"] == 2
    assert registry["summary"]["synced"] + registry["summary"]["attention"] == 2

    # Isolate the reconfirmation case from the other card: this assertion is
    # about retaining the connected date on an unchanged card.
    cards["partial"].is_active = False
    db.commit()
    first_connected_at = cards["multi-group"].first_connected_at
    sync_asoc_products(
        db,
        [{
            "id": "multi-group",
            "name": "ASOC example_app card (BI_2)",
            "description": (
                "https://gitlab.example/sandbox/example_app/api\n"
                "https://gitlab.example/sandbox/identity/api"
            ),
        }],
    )
    assert db.query(AsocProduct).filter_by(asoc_id="multi-group").one().first_connected_at == first_connected_at


def test_product_mapping_keeps_ambiguous_exact_url_in_conflict():
    db = _db()
    db.add_all([
        Product(name="example_app-a", path="example_app", gitlab_base_url="https://gitlab.example"),
        Product(name="example_app-b", path="example_app", gitlab_base_url="https://gitlab.example"),
    ])
    db.commit()

    sync_asoc_products(db, [{
        "id": "ambiguous",
        "name": "ExampleApp (BI_2)",
        "description": "https://gitlab.example/example_app",
    }])

    card = db.query(AsocProduct).one()
    assert card.mapping_state == ASOC_MATCH_CONFLICT
    assert card.first_connected_at is None
    assert db.query(AsocProductRepositoryMapping).count() == 0


def test_two_asoc_cards_claiming_one_target_create_no_authoritative_mapping():
    db = _db()
    target = Product(
        name="example_app",
        path="sandbox/example_app",
        gitlab_base_url="https://gitlab.example",
    )
    db.add(target)
    db.flush()
    db.add(Repository(product_id=target.id, path="sandbox/example_app/api"))
    db.commit()

    sync_asoc_products(db, [
        {
            "id": "asoc-one",
            "name": "ExampleApp one (BI_1)",
            "description": "https://gitlab.example/sandbox/example_app/api",
        },
        {
            "id": "asoc-two",
            "name": "ExampleApp two (BI_2)",
            "description": "https://gitlab.example/sandbox/example_app/api",
        },
    ])

    cards = {row.asoc_id: row for row in db.query(AsocProduct).all()}
    db.refresh(target)
    assert {row.mapping_state for row in cards.values()} == {ASOC_MATCH_CONFLICT}
    assert all(row.monitor_product_id is None for row in cards.values())
    assert db.query(AsocProductRepositoryMapping).count() == 0
    assert target.asoc_product_id is None
    assert target.asoc_sync_status == ASOC_MATCH_CONFLICT
    report = report_rows(db)
    assert {row["mapping_state"] for row in report} == {ASOC_MATCH_CONFLICT}
    assert all("description" not in row for row in report)
    assert all(row["monitor_products"] == [] for row in report)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("https://gitlab.example/group\nScanOps-Scope: internal", "internal"),
        ("ScanOps-Scope: EXTERNAL", "external"),
        ("Internal product, no controlled marker", "unclassified"),
        ("ScanOps-Scope: internal\nScanOps-Scope: external", "conflict"),
    ],
)
def test_product_scope_uses_only_explicit_asoc_description_marker(description, expected):
    assert product_scope(description) == expected


def test_asoc_product_url_uses_public_host_and_encodes_slug():
    assert asoc_product_url(
        "product-id",
        "example_app / web",
        base_url="https://asoc.example/api",
    ) == "https://asoc.example/products/example_app%20%2F%20web"


def test_product_mapping_does_not_fallback_to_asoc_product_id_without_description_link():
    db = _db()
    product = Product(
        name="example_app",
        path="sandbox/example_app",
        gitlab_base_url="https://gitlab.example",
        asoc_product_id="asoc-example_app",
    )
    db.add(product)
    db.commit()

    sync_asoc_products(
        db,
        [{
            "id": "asoc-example_app",
            "name": "ExampleApp (BI_77)",
            "slug": "example_app",
            "description": "No repository link here",
        }],
    )

    card = db.query(AsocProduct).one()
    db.refresh(product)
    assert card.monitor_product_id is None
    assert product.asoc_product_id is None


def test_product_sync_aborts_on_duplicate_cards_before_deleting_existing_cards():
    db = _db()
    db.add(AsocProduct(asoc_id="existing", name="Existing product"))
    db.commit()

    duplicate_card = {
        "id": "asoc-example_app",
        "name": "ExampleApp (BI_77)",
        "slug": "example_app",
        "description": "https://gitlab.example/sandbox/example_app",
    }
    with pytest.raises(RuntimeError, match="duplicate product id"):
        sync_asoc_products(db, [duplicate_card, dict(duplicate_card)])

    cards = {card.asoc_id: card for card in db.query(AsocProduct).all()}
    assert set(cards) == {"existing"}
    assert cards["existing"].name == "Existing product"


def test_incomplete_product_snapshot_does_not_delete_existing_cards(monkeypatch):
    db = _db()
    db.add(AsocProduct(asoc_id="existing", name="Existing product"))
    db.commit()

    class IncompleteClient:
        def enabled(self):
            return True

        def list_products(self):
            raise RuntimeError("ASOC products reached the safety limit; refusing an incomplete snapshot")

    monkeypatch.setattr("app.asoc_products.AsocClient", IncompleteClient)

    with pytest.raises(RuntimeError, match="incomplete snapshot"):
        fetch_and_sync(db)

    assert db.query(AsocProduct).filter_by(asoc_id="existing").one().name == "Existing product"


def test_fetch_and_sync_rejects_overlapping_local_sync(monkeypatch):
    class EnabledClient:
        def enabled(self):
            return True

        def list_products(self):
            raise AssertionError("a rejected overlapping sync must not fetch")

    monkeypatch.setattr("app.asoc_products.AsocClient", EnabledClient)
    assert _ASOC_SYNC_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(AsocSyncInProgressError):
            fetch_and_sync(_db())
    finally:
        _ASOC_SYNC_LOCK.release()


def test_empty_snapshot_requires_confirmation_and_preserves_cards_until_confirmed():
    db = _db()
    db.add(AsocProduct(asoc_id="existing", name="Existing product"))
    db.commit()

    with pytest.raises(AsocSyncConfirmationRequiredError) as raised:
        sync_asoc_products(db, [])

    card = db.query(AsocProduct).filter_by(asoc_id="existing").one()
    assert card.is_active is True
    assert card.removed_at is None
    confirmation = raised.value
    with pytest.raises(AsocSyncConfirmationRequiredError) as stale:
        sync_asoc_products(
            db,
            [],
            confirm_mass_removal=True,
            expected_active_count=confirmation.active_count,
            expected_incoming_count=confirmation.incoming_count,
            expected_registry_version="0" * 64,
        )
    assert stale.value.stale_confirmation is True
    assert db.query(AsocProduct).filter_by(asoc_id="existing").one().is_active is True

    result = sync_asoc_products(
        db,
        [],
        confirm_mass_removal=True,
        expected_active_count=confirmation.active_count,
        expected_incoming_count=confirmation.incoming_count,
        expected_registry_version=confirmation.registry_version,
    )
    assert result["removed"] == 1
    assert result["confirmation_used"] == 1
    event = db.query(AsocProductSyncEvent).one()
    assert event.removed_count == 1
    assert event.confirmed is True


def test_mass_removal_confirmation_is_bound_to_exact_incoming_asoc_ids():
    db = _db()
    db.add_all(AsocProduct(asoc_id=f"asoc-{idx}", name=f"Product {idx}") for idx in range(4))
    db.commit()
    first_snapshot = [{"id": "asoc-0", "name": "Product 0"}]

    with pytest.raises(AsocSyncConfirmationRequiredError) as raised:
        sync_asoc_products(db, first_snapshot)
    confirmation = raised.value
    with pytest.raises(AsocSyncConfirmationRequiredError) as stale:
        sync_asoc_products(
            db,
            [{"id": "different-card", "name": "Different card"}],
            confirm_mass_removal=True,
            expected_active_count=confirmation.active_count,
            expected_incoming_count=confirmation.incoming_count,
            expected_registry_version=confirmation.registry_version,
        )
    assert stale.value.stale_confirmation is True
    assert db.query(AsocProduct).filter(AsocProduct.is_active.is_(True)).count() == 4

    result = sync_asoc_products(
        db,
        first_snapshot,
        confirm_mass_removal=True,
        expected_active_count=confirmation.active_count,
        expected_incoming_count=confirmation.incoming_count,
        expected_registry_version=confirmation.registry_version,
    )
    assert result["removed"] == 3
    event = db.query(AsocProductSyncEvent).one()
    assert event.incoming_count == 1
    assert event.confirmed is True


def test_legacy_lifecycle_guard_prevents_physical_asoc_product_deletion():
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    card = AsocProduct(asoc_id="protected", name="Protected product")
    db.add(card)
    db.flush()
    db.add(AsocProductLifecycleGuard(asoc_id=card.asoc_id))
    db.commit()

    db.delete(card)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.query(AsocProduct).filter_by(asoc_id="protected").one()


def test_removed_asoc_card_is_retained_for_quarterly_reporting_and_reactivation():
    db = _db()
    target = Product(
        name="example_app",
        path="sandbox/example_app",
        gitlab_base_url="https://gitlab.example",
    )
    db.add(target)
    db.flush()
    db.add(Repository(product_id=target.id, path="sandbox/example_app/api"))
    db.commit()
    mapped = {
        "id": "example_app-card",
        "name": "ExampleApp (BI_77)",
        "description": "https://gitlab.example/sandbox/example_app/api",
    }
    keeper = {"id": "keeper", "name": "Other product (BI_78)", "description": ""}
    sync_asoc_products(db, [mapped, keeper])
    first_connected = db.query(AsocProduct).filter_by(asoc_id="example_app-card").one().first_connected_at

    with pytest.raises(AsocSyncConfirmationRequiredError) as raised:
        sync_asoc_products(db, [keeper])
    confirmation = raised.value
    sync_asoc_products(
        db,
        [keeper],
        confirm_mass_removal=True,
        expected_active_count=confirmation.active_count,
        expected_incoming_count=confirmation.incoming_count,
        expected_registry_version=confirmation.registry_version,
    )

    historic = db.query(AsocProduct).filter_by(asoc_id="example_app-card").one()
    assert historic.is_active is False
    assert historic.removed_at is not None
    assert historic.first_connected_at == first_connected
    historical_row = next(row for row in report_rows(db) if row["asoc_id"] == "example_app-card")
    assert historical_row["mapping_state"] == "removed"
    assert historical_row["first_connected_at"] == first_connected
    assert historical_row["monitor_product"] == target
    metadata = asoc_sync_metadata(db, configured=True)
    assert metadata["total_cards"] == 1
    assert metadata["historical_cards"] == 1

    with pytest.raises(AsocSyncConfirmationRequiredError) as reactivation_prompt:
        sync_asoc_products(db, [mapped])
    confirmation = reactivation_prompt.value
    sync_asoc_products(
        db,
        [mapped],
        confirm_mass_removal=True,
        expected_active_count=confirmation.active_count,
        expected_incoming_count=confirmation.incoming_count,
        expected_registry_version=confirmation.registry_version,
    )
    restored = db.query(AsocProduct).filter_by(asoc_id="example_app-card").one()
    assert restored.is_active is True
    assert restored.removed_at is None
    assert restored.first_connected_at == first_connected


def test_unconfirmed_small_removal_leaves_a_lasting_audit_event():
    db = _db()
    cards = [{"id": f"card-{index}", "name": f"Card {index} (BI_{index})", "description": ""} for index in range(5)]
    sync_asoc_products(db, cards)

    # One of five is below the confirmation threshold, so an operator's sync
    # applies it without a prompt; the audit row still records it.
    result = sync_asoc_products(db, cards[1:], confirmed_by="operator")

    assert result["removed"] == 1
    event = db.query(AsocProductSyncEvent).one()
    assert (event.actor, event.confirmed, event.removed_count, event.previous_active_count) == (
        "operator",
        False,
        1,
        5,
    )


def test_snapshot_without_created_keeps_the_quarter_placement():
    db = _db()
    created = "2026-07-03T10:00:00Z"
    sync_asoc_products(db, [{"id": "dated", "name": "Dated (BI_1)", "description": "", "created": created}])
    stored = db.query(AsocProduct).filter_by(asoc_id="dated").one().asoc_created

    for garbled in (None, "", "not-a-date"):
        sync_asoc_products(db, [{"id": "dated", "name": "Dated (BI_1)", "description": "", "created": garbled}])
        assert db.query(AsocProduct).filter_by(asoc_id="dated").one().asoc_created == stored

    sync_asoc_products(db, [{"id": "dated", "name": "Dated (BI_1)", "description": "", "created": "2026-08-01T00:00:00Z"}])
    assert db.query(AsocProduct).filter_by(asoc_id="dated").one().asoc_created.month == 8


def test_stored_implausible_creation_date_is_cleared_by_the_next_sync():
    db = _db()
    db.add(AsocProduct(asoc_id="garbage", name="Garbage (BI_9)", asoc_created=datetime(9999, 12, 31, tzinfo=timezone.utc)))
    db.commit()

    sync_asoc_products(db, [{"id": "garbage", "name": "Garbage (BI_9)", "description": ""}])

    assert db.query(AsocProduct).filter_by(asoc_id="garbage").one().asoc_created is None
    assert next(row for row in report_rows(db) if row["asoc_id"] == "garbage")["asoc_created"] is None


def test_scheduled_sync_never_archives_even_a_single_card(monkeypatch):
    db = _db()
    cards = [{"id": f"card-{index}", "name": f"Card {index} (BI_{index})", "description": ""} for index in range(5)]
    sync_asoc_products(db, cards)
    new_card = {"id": "card-new", "name": "New (BI_99)", "description": ""}

    class _Client:
        def enabled(self):
            return True

        def list_products(self):
            return [*cards[1:], new_card]

    monkeypatch.setattr("app.asoc_products.AsocClient", _Client)

    outcome = run_scheduled_sync(db)

    assert outcome["status"] == "needs_confirmation"
    assert "Automatic sync never removes products" in outcome["message"]
    assert db.query(AsocProduct).filter(AsocProduct.is_active.is_(True)).count() == 5
    # The whole snapshot waits for the operator, including the new card.
    assert db.query(AsocProduct).filter_by(asoc_id="card-new").count() == 0
    assert db.query(AsocProductSyncEvent).count() == 0

    # The operator's sync applies the small removal without a prompt.
    result = sync_asoc_products(db, [*cards[1:], new_card], confirmed_by="operator")
    assert (result["removed"], result["created"]) == (1, 1)


def test_scheduled_sync_never_archives_a_mass_removal_end_to_end(monkeypatch):
    db = _db()
    cards = [{"id": f"card-{index}", "name": f"Card {index} (BI_{index})", "description": ""} for index in range(4)]
    sync_asoc_products(db, cards)
    snapshots = iter([[], cards[:1]])

    class _Client:
        def enabled(self):
            return True

        def list_products(self):
            return next(snapshots)

    monkeypatch.setattr("app.asoc_products.AsocClient", _Client)

    for _snapshot in range(2):  # an empty catalog, then three of four cards gone
        outcome = run_scheduled_sync(db)
        assert outcome["status"] == "needs_confirmation"
        assert db.query(AsocProduct).filter(AsocProduct.is_active.is_(True)).count() == 4
    assert db.query(AsocProductSyncEvent).count() == 0


def test_scheduled_sync_failure_never_stores_upstream_error_text(monkeypatch):
    db = _db()

    class _Enabled:
        def enabled(self):
            return True

    def _fail(*_args, **_kwargs):
        raise RuntimeError("ASOC HTTP 401 for https://asoc.example/api?token=secret-value")

    monkeypatch.setattr("app.asoc_products.AsocClient", _Enabled)
    monkeypatch.setattr("app.asoc_products.fetch_and_sync", _fail)

    outcome = run_scheduled_sync(db)

    assert outcome["status"] == "failed"
    assert "secret-value" not in outcome["message"]
    assert outcome["message"].startswith("ASOC synchronization failed (reference ")
    assert read_sync_outcome(db)["message"] == outcome["message"]


def test_first_sync_outcome_survives_a_concurrent_first_write(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    web, worker = Session(), Session()
    # Both sessions look before either writes: the worker finds no row yet.
    assert worker.query(AppState).filter(AppState.key == ASOC_SYNC_STATE_KEY).first() is None
    record_sync_outcome(web, trigger="operator", status="ok", message="web first")

    original_query = worker.query
    calls = {"count": 0}

    def _stale_first_lookup(*args, **kwargs):
        query = original_query(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            return query.filter(AppState.key == "__never__")
        return query

    worker.query = _stale_first_lookup
    record_sync_outcome(worker, trigger="scheduler", status="ok", message="worker second")

    assert read_sync_outcome(Session())["message"] == "worker second"


def test_an_attempt_that_started_earlier_never_hides_a_newer_outcome():
    db = _db()
    newer = datetime(2026, 9, 26, 12, 5, tzinfo=timezone.utc)
    older = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    record_sync_outcome(db, trigger="operator", status="ok", message="confirmed", at=newer)
    record_sync_outcome(db, trigger="scheduler", status="needs_confirmation", message="stale", at=older)

    assert read_sync_outcome(db)["message"] == "confirmed"


def test_an_older_attempt_losing_the_first_write_race_keeps_the_newer_outcome(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    web, worker = Session(), Session()
    newer = datetime(2026, 9, 26, 12, 5, tzinfo=timezone.utc)
    older = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    record_sync_outcome(web, trigger="operator", status="ok", message="newer", at=newer)

    original_query = worker.query
    calls = {"count": 0}

    def _stale_first_lookup(*args, **kwargs):
        query = original_query(*args, **kwargs)
        calls["count"] += 1
        return query.filter(AppState.key == "__never__") if calls["count"] == 1 else query

    worker.query = _stale_first_lookup
    record_sync_outcome(worker, trigger="scheduler", status="failed", message="older", at=older)

    assert read_sync_outcome(Session())["message"] == "newer"


def test_reactivating_undated_legacy_tombstone_does_not_invent_connection_date():
    db = _db()
    target = Product(
        name="legacy-target",
        path="legacy-target",
        gitlab_base_url="[URL_HIDDEN]",
    )
    db.add(target)
    db.flush()
    db.add(Repository(product_id=target.id, path="legacy-target/repo"))
    db.flush()
    db.add(
        AsocProduct(
            asoc_id="legacy-tombstone",
            name="Legacy product",
            mapping_state="removed",
            is_active=False,
            monitor_product_id=target.id,
            first_connected_at=None,
        )
    )
    db.commit()

    sync_asoc_products(
        db,
        [{"id": "legacy-tombstone", "name": "Legacy product", "description": "[URL_HIDDEN]"}],
    )

    restored = db.query(AsocProduct).filter_by(asoc_id="legacy-tombstone").one()
    assert restored.is_active is True
    assert restored.first_connected_at is None


def test_legacy_link_survives_unlink_and_relink_without_a_new_connection_date():
    db = _db()
    target = Product(name="legacy", path="sandbox/legacy", gitlab_base_url="https://gitlab.example")
    db.add(target)
    db.flush()
    db.add(Repository(product_id=target.id, path="sandbox/legacy/api"))
    # Linked before first_connected_at existed: mapped, no date.
    db.add(AsocProduct(asoc_id="legacy-card", name="Legacy (BI_5)", mapping_state="mapped",
                       monitor_product_id=target.id, first_connected_at=None))
    db.commit()
    linked = {"id": "legacy-card", "name": "Legacy (BI_5)", "description": "https://gitlab.example/sandbox/legacy"}
    unlinked = {**linked, "description": "https://gitlab.example/sandbox/elsewhere"}

    for snapshot in (linked, unlinked, linked):
        sync_asoc_products(db, [snapshot])

    card = db.query(AsocProduct).filter_by(asoc_id="legacy-card").one()
    assert card.monitor_product_id == target.id
    assert card.first_connected_at is None
    assert card.connected_before_tracking is True


def test_new_card_is_dated_and_not_marked_as_pre_tracking():
    db = _db()
    target = Product(name="fresh", path="sandbox/fresh", gitlab_base_url="https://gitlab.example")
    db.add(target)
    db.flush()
    db.add(Repository(product_id=target.id, path="sandbox/fresh/api"))
    db.commit()

    sync_asoc_products(db, [{"id": "fresh-card", "name": "Fresh (BI_6)", "description": "https://gitlab.example/sandbox/fresh"}])

    card = db.query(AsocProduct).filter_by(asoc_id="fresh-card").one()
    assert card.first_connected_at is not None
    assert card.connected_before_tracking is False


def test_latest_successful_pipeline_ignores_newer_failed_pipeline():
    db = _db()
    product = Product(
        name="example_app",
        path="sandbox/example_app",
        gitlab_base_url="https://gitlab.example",
    )
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="sandbox/example_app/api")
    db.add(repository)
    db.flush()
    now = datetime.now(timezone.utc)
    successful_at = now - timedelta(hours=4)
    db.add_all(
        [
            ActivePipeline(
                project_id=1,
                pipeline_id=10,
                product_id=product.id,
                repository_id=repository.id,
                status=PipelineStatus.COMPLETED,
                completed_at=successful_at,
            ),
            ActivePipeline(
                project_id=1,
                pipeline_id=11,
                product_id=product.id,
                repository_id=repository.id,
                status=PipelineStatus.FAILED,
                completed_at=now,
            ),
        ]
    )
    db.commit()

    db.add(AsocProduct(
        asoc_id="example_app",
        name="ExampleApp (BI_1)",
        title="ExampleApp",
        monitor_product_id=product.id,
        mapping_state=ASOC_MATCH_MAPPED,
        asoc_created=now - timedelta(days=10),
    ))
    db.commit()

    row = build_registry(db, RegistryQuery(), now=now)["rows"][0]

    assert row["pipelines"]["last_success_at"] == successful_at.isoformat()
    assert row["pipelines"]["repos_with_pipelines"] == 1
    assert row["pipelines"]["repos_green"] == 0


def test_product_registry_keeps_asoc_only_rows_and_pipeline_filter_link():
    db = _db()
    product = Product(
        name="example_app",
        path="sandbox/example_app",
        gitlab_base_url="https://gitlab.example",
        asoc_product_id="asoc-example_app",
        asoc_sync_status=ASOC_MATCH_MAPPED,
    )
    db.add(product)
    db.flush()
    repository = Repository(product_id=product.id, path="sandbox/example_app/api")
    db.add(repository)
    db.flush()
    successful_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.add_all(
        [
            AsocProduct(
                asoc_id="asoc-example_app",
                name="ExampleApp (BI_77)",
                title="ExampleApp",
                bi_id="77",
                monitor_product_id=product.id,
                mapping_state=ASOC_MATCH_MAPPED,
                repositories_count=1,
                matched_repositories_count=1,
            ),
            AsocProduct(
                asoc_id="asoc-orphan",
                name="Orphan (BI_88)",
                title="Orphan",
                bi_id="88",
                mapping_state="missing_target",
                repositories_count=3,
            ),
            ActivePipeline(
                project_id=1,
                pipeline_id=20,
                product_id=product.id,
                repository_id=repository.id,
                status=PipelineStatus.COMPLETED,
                completed_at=successful_at,
            ),
        ]
    )
    db.commit()

    payload = build_registry(db, RegistryQuery())
    by_key = {row["key"]: row for row in payload["rows"]}

    mapped = by_key["asoc:asoc-example_app"]
    assert mapped["name"] == "ExampleApp"
    assert mapped["status"] == "synced"
    assert [group["pipelines_href"] for group in mapped["groups"]] == [f"/pipelines?product={product.id}"]
    assert mapped["pipelines"]["last_success_at"].startswith(
        successful_at.replace(tzinfo=None).isoformat(timespec="seconds")
    )

    orphan = by_key["asoc:asoc-orphan"]
    assert orphan["groups"] == []
    assert orphan["status"] == "group_not_monitored"
    assert payload["summary"]["attention"] == 1


def test_product_registry_keeps_same_bi_id_asoc_cards_separate():
    db = _db()
    targets = [
        Product(name=f"group-{index}", path=f"group-{index}", gitlab_base_url="https://gitlab.example")
        for index in (1, 2)
    ]
    db.add_all(targets)
    db.flush()
    db.add_all([
        AsocProduct(
            asoc_id=f"same-bi-{index}",
            name=f"Product {index} (BI_77)",
            title=f"Product {index}",
            bi_id="77",
            monitor_product_id=target.id,
            mapping_state=ASOC_MATCH_MAPPED,
        )
        for index, target in enumerate(targets, start=1)
    ])
    db.commit()

    payload = build_registry(db, RegistryQuery())

    assert {row["asoc_id"] for row in payload["rows"]} == {"same-bi-1", "same-bi-2"}
    assert payload["summary"]["total"] == 2
