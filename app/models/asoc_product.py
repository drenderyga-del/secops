from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func, text

from .database import Base


class AsocProduct(Base):
    """Product card synced from ASOC (`GET /ext/v1/products/`).

    ASOC products are created by hand. ``bi_id`` / ``title`` are parsed out of
    ``name`` for display and reporting, while the ScanOps mapping is resolved
    only from GitLab links in ``description``.
    """

    __tablename__ = "asoc_products"

    id = Column(Integer, primary_key=True)
    asoc_id = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=True, index=True)
    # Parsed from name: "Product name (BI_243)" -> title + "243".
    bi_id = Column(String(32), nullable=True, index=True)
    title = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    repositories_count = Column(Integer, nullable=False, default=0)
    matched_repositories_count = Column(Integer, nullable=False, default=0)
    mapping_state = Column(String(32), nullable=False, default="not_synced", index=True)
    mapping_detail = Column(Text, nullable=True)
    monitor_product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    asoc_created = Column(DateTime(timezone=True), nullable=True)
    # Set on the first confirmed ASOC-description-to-ScanOps mapping after
    # lifecycle tracking is introduced. Historical links without evidence of
    # when they were first connected intentionally remain NULL.
    first_connected_at = Column(DateTime(timezone=True), nullable=True)
    # Durable evidence that a card was linked before first_connected_at was
    # tracked: it keeps a later unlink/relink from stamping a new date.
    connected_before_tracking = Column(Boolean, nullable=False, default=False, server_default="false")
    # Keep cards that disappear from ASOC as tombstones so quarterly reports
    # retain their first-connected date and last known ScanOps mapping.
    is_active = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    removed_at = Column(DateTime(timezone=True), nullable=True)
    synced_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AsocProductRepositoryMapping(Base):
    """One exact ASOC description URL resolved to a monitored target.

    A canonical ASOC product can span multiple GitLab product groups. Keeping
    each verified URL mapping lets Product 360 show that relationship without
    pretending the business product is several products.
    """

    __tablename__ = "asoc_product_repository_mappings"
    __table_args__ = (
        UniqueConstraint(
            "asoc_id", "description_url", name="uq_asoc_product_description_mapping"
        ),
    )

    id = Column(Integer, primary_key=True)
    asoc_id = Column(
        String,
        ForeignKey("asoc_products.asoc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description_url = Column(String(2048), nullable=False)
    repository_id = Column(
        Integer,
        ForeignKey("repositories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class AsocProductLifecycleGuard(Base):
    """Restrict legacy releases from physically deleting ASOC lifecycle rows."""

    __tablename__ = "asoc_product_lifecycle_guards"

    asoc_id = Column(
        String,
        ForeignKey("asoc_products.asoc_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AsocProductSyncEvent(Base):
    """Audit evidence for every sync that archived ASOC product cards.

    ``confirmed`` marks a mass removal an operator explicitly approved; other
    rows are removals below the confirmation threshold, including the ones the
    scheduled sync applies unattended.
    """

    __tablename__ = "asoc_product_sync_events"
    __table_args__ = (
        CheckConstraint("previous_active_count >= 0", name="ck_asoc_sync_previous_nonnegative"),
        CheckConstraint("incoming_count >= 0", name="ck_asoc_sync_incoming_nonnegative"),
        CheckConstraint("removed_count >= 0", name="ck_asoc_sync_removed_nonnegative"),
    )

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    actor = Column(String(120), nullable=False, default="operator")
    previous_active_count = Column(Integer, nullable=False)
    incoming_count = Column(Integer, nullable=False)
    removed_count = Column(Integer, nullable=False)
    registry_version = Column(String(64), nullable=False)
    # The server default keeps pre-migration rows (all operator-confirmed)
    # truthful; new rows must state it, so the ORM default is the safe side.
    confirmed = Column(Boolean, nullable=False, default=False, server_default=text("true"))
