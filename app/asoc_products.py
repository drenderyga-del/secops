"""Sync the canonical ASOC product cards and enrich them with ScanOps links.

The ASOC ID is the product-card identity; BI_ID is reporting metadata, not a
deduplication key. Exact GitLab URLs in a card's Description can resolve to
one or more monitored product groups. Mapping evidence and first-confirmed
connection dates are retained for reporting without merging distinct ASOC
cards or inventing historical dates.
"""
from __future__ import annotations

import json
import logging
import hashlib
import os
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .bi_identity import normalize_bi_id
from .asoc_description_safety import (
    description_reference_urls,
    safe_asoc_public_base,
    safe_description_links,
    safe_description_url,
    sanitize_description,
)
from .iac_asoc import AsocClient, normalize_repo_url
from .alerting.ptai_scan_status import asoc_public_url
from .models import (
    AppState,
    AsocProduct,
    AsocProductRepositoryMapping,
    AsocProductSyncEvent,
    Product,
    Repository,
)

logger = logging.getLogger(__name__)

# "Product name (BI_243)" - tolerate BI-243 / bi 243 / (BI_243 ) too.
# Some ASOC cards have a composite reporting identity such as
# ``(BI_162/BI_2184)``. BI_ID labels reports but never drives product mapping.
_BI_RE = re.compile(
    r"\(\s*BI[\s_-]*(\d+(?:(?:\s*[/,]\s*)(?:BI[\s_-]*)?\d+)*)\s*\)",
    re.IGNORECASE,
)
_SCOPE_RE = re.compile(r"^\s*scanops-scope\s*:\s*(internal|external)\s*$", re.IGNORECASE | re.MULTILINE)

MAPPING_MAPPED = "mapped"
MAPPING_MISSING_TARGET = "missing_target"
MAPPING_MISSING_BI_ID = "missing_bi_id"

ASOC_MATCH_MAPPED = "mapped"
ASOC_MATCH_PARTIAL = "partial"
ASOC_MATCH_CONFLICT = "conflict"
ASOC_MATCH_MISSING_TARGET = "missing_target"
ASOC_MATCH_NO_REPOSITORIES = "no_repositories"
ASOC_MATCH_NOT_SYNCED = "not_synced"
ASOC_MATCH_REMOVED = "removed"
ASOC_SYNC_STALE_AFTER = timedelta(hours=24)
ASOC_SYNC_ADVISORY_LOCK_ID = 0x41534F4353594E43
ASOC_LIFECYCLE_WRITE_SETTING = "scanops.asoc_product_lifecycle_write"
ASOC_SYNC_STATE_KEY = "asoc_product_sync"
_ASOC_SYNC_LOCK = threading.Lock()


def enable_asoc_lifecycle_writes(db: Session) -> None:
    """Mark the current PostgreSQL transaction as using lifecycle-aware writes.

    A database trigger uses this transaction-local marker to fail closed if an
    older application release is rolled back after the lifecycle migration.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT set_config(:setting, 'allowed', true)"),
            {"setting": ASOC_LIFECYCLE_WRITE_SETTING},
        )


class AsocSyncInProgressError(RuntimeError):
    """Another ASOC product snapshot is currently being applied."""


class AsocSyncConfirmationRequiredError(RuntimeError):
    """An unusually large ASOC removal needs a current operator confirmation."""

    def __init__(
        self,
        *,
        active_count: int,
        incoming_count: int,
        removal_count: int,
        registry_version: str,
        stale_confirmation: bool = False,
    ) -> None:
        super().__init__("ASOC snapshot requires explicit product-removal confirmation")
        self.active_count = active_count
        self.incoming_count = incoming_count
        self.removal_count = removal_count
        self.registry_version = registry_version
        self.stale_confirmation = stale_confirmation


def parse_bi_id(name: str | None) -> tuple[Optional[str], str]:
    """Extract the BI id from an ASOC product name.

    Returns ``(bi_id | None, title)`` where *title* is the name with the
    ``(BI_…)`` marker stripped. The LAST marker wins when a name accidentally
    contains several.
    """
    name = (name or "").strip()
    if not name:
        return None, ""
    matches = list(_BI_RE.finditer(name))
    if not matches:
        return None, name
    last = matches[-1]
    title = (name[: last.start()] + name[last.end():]).strip(" -–—·|")
    return normalize_bi_id(last.group(1)), title or name


# ASOC ``created`` drives quarterly reporting; values outside this window are
# upstream garbage and would otherwise overflow quarter arithmetic.
CREATED_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
CREATED_MAX = datetime(2100, 1, 1, tzinfo=timezone.utc)


def plausible_created(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value if CREATED_MIN <= value < CREATED_MAX else None


def _parse_created(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return plausible_created(dt)


def _monitored_repository_url(product: Product, repository: Repository) -> str:
    path = (repository.path or "").strip()
    if path.startswith(("http://", "https://")):
        return normalize_repo_url(path)
    base = (product.gitlab_base_url or "").strip().rstrip("/")
    return normalize_repo_url(f"{base}/{path.lstrip('/')}") if base and path else ""


def _monitored_product_url(product: Product) -> str:
    base = (product.gitlab_base_url or "").strip().rstrip("/")
    path = (product.path or "").strip().strip("/")
    return normalize_repo_url(f"{base}/{path}") if base and path else ""


def description_identity_urls(description: str | None) -> list[str]:
    """Extract normalized HTTP(S) links from an ASOC product description."""
    urls: list[str] = []
    for raw in description_reference_urls(description):
        safe_url = safe_description_url(raw)
        normalized = normalize_repo_url(safe_url)
        if normalized and normalized not in urls:
            urls.append(normalized)
    return urls


def product_scope(description: str | None) -> str:
    """Read an explicit reporting classification from the canonical ASOC card.

    The marker is intentionally exact and machine-readable so ordinary prose
    or a GitLab URL can never be used to guess the product's audience.
    """
    values = {value.casefold() for value in _SCOPE_RE.findall(description or "")}
    if len(values) == 1:
        return next(iter(values))
    return "conflict" if values else "unclassified"


def asoc_product_url(
    asoc_id: str | None,
    slug: str | None = None,
    *,
    base_url: str | None = None,
) -> str | None:
    """Build the ASOC product-card URL from the configured public base."""
    identity = str(slug or asoc_id or "").strip()
    base = safe_asoc_public_base(asoc_public_url(base_url))
    if not identity or not base:
        return None
    return f"{base.rstrip('/')}/products/{quote(identity, safe='')}"


def _description_repository_urls(
    description: str | None,
    local_by_url: dict[str, set[int]],
) -> list[str]:
    """Keep GitLab links, plus exact matches for non-standard GitLab hosts."""
    result: list[str] = []
    for url in description_identity_urls(description):
        host = url.split("/", 3)[2] if "://" in url else ""
        if "gitlab" in host or url in local_by_url:
            result.append(url)
    return result


def _apply_repository_mappings(
    db: Session,
    cards: list[AsocProduct],
) -> dict[str, int]:
    """Map ASOC products by exact GitLab links from their descriptions."""
    products = db.query(Product).order_by(Product.id.asc()).all()
    product_by_id = {product.id: product for product in products}
    local_by_url: dict[str, set[int]] = defaultdict(set)
    repositories_by_url_and_product: dict[str, dict[int, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for product in products:
        identity = _monitored_product_url(product)
        if identity:
            local_by_url[identity].add(product.id)
    for repository, product in db.query(Repository, Product).join(
        Product, Repository.product_id == Product.id
    ):
        identity = _monitored_repository_url(product, repository)
        if identity:
            local_by_url[identity].add(product.id)
            repositories_by_url_and_product[identity][product.id].add(repository.id)

    asoc_ids = [card.asoc_id for card in cards]
    previously_mapped_asoc_ids: set[str] = set()
    if asoc_ids:
        previously_mapped_asoc_ids = {
            asoc_id
            for (asoc_id,) in db.query(AsocProductRepositoryMapping.asoc_id)
            .filter(AsocProductRepositoryMapping.asoc_id.in_(asoc_ids))
            .distinct()
        }
        db.query(AsocProductRepositoryMapping).filter(
            AsocProductRepositoryMapping.asoc_id.in_(asoc_ids)
        ).delete(synchronize_session="fetch")

    candidate_cards: dict[int, list[AsocProduct]] = defaultdict(list)
    target_ids_by_card: dict[str, set[int]] = defaultdict(set)
    conflicted_product_ids: set[int] = set()
    was_connected: dict[str, bool] = {}
    for card in cards:
        # A tombstone still carries historical mapping evidence. Do not invent
        # a first-connected date if a legacy row was reactivated without one.
        was_connected[card.asoc_id] = (
            card.first_connected_at is not None
            or card.connected_before_tracking
            or card.monitor_product_id is not None
            or card.asoc_id in previously_mapped_asoc_ids
        )
        if was_connected[card.asoc_id] and card.first_connected_at is None:
            # Never cleared: the pointer and mapping rows below are rebuilt on
            # every sync and would otherwise lose this history.
            card.connected_before_tracking = True
        description_urls = _description_repository_urls(card.description, local_by_url)
        card.repositories_count = len(description_urls)
        card.matched_repositories_count = 0
        card.monitor_product_id = None
        matched_product_ids: set[int] = set()
        ambiguous_link = False

        for identity in description_urls:
            local_matches = local_by_url.get(identity, set())
            if len(local_matches) == 1:
                card.matched_repositories_count += 1
                product_id = next(iter(local_matches))
                matched_product_ids.add(product_id)
                target_ids_by_card[card.asoc_id].add(product_id)
                candidate_cards[product_id].append(card)
                matching_repository_ids = repositories_by_url_and_product.get(
                    identity, {}
                ).get(product_id, set())
                db.add(
                    AsocProductRepositoryMapping(
                        asoc_id=card.asoc_id,
                        product_id=product_id,
                        description_url=identity,
                        repository_id=(
                            min(matching_repository_ids)
                            if len(matching_repository_ids) == 1
                            else None
                        ),
                    )
                )
            elif len(local_matches) > 1:
                ambiguous_link = True
                conflicted_product_ids.update(local_matches)

        matched = card.matched_repositories_count
        total = card.repositories_count
        if total == 0:
            card.mapping_state = ASOC_MATCH_NO_REPOSITORIES
            card.mapping_detail = "ASOC product description contains no GitLab links."
        elif ambiguous_link:
            card.mapping_state = ASOC_MATCH_CONFLICT
            card.mapping_detail = (
                f"{matched} of {total} description links matched; at least one exact "
                "URL resolves to more than one monitored product."
            )
        elif not matched_product_ids:
            card.mapping_state = ASOC_MATCH_MISSING_TARGET
            card.mapping_detail = (
                f"0 of {total} ASOC description links matched an exact monitored GitLab URL."
            )
        else:
            product_ids = sorted(matched_product_ids)
            card.mapping_state = (
                ASOC_MATCH_MAPPED if matched == total else ASOC_MATCH_PARTIAL
            )
            card.mapping_detail = (
                f"{matched} of {total} ASOC description links matched across "
                f"{len(product_ids)} monitored product group(s) by exact GitLab URL."
            )
            # Keep the legacy singular pointer deterministic for older readers;
            # the mapping table is authoritative when a product spans groups.
            card.monitor_product_id = product_ids[0]
            for product_id in product_ids:
                target_ids_by_card[card.asoc_id].add(product_id)

    # One monitored target cannot silently prove two different ASOC cards.
    for product_id, candidates in candidate_cards.items():
        distinct_candidates = {candidate.asoc_id: candidate for candidate in candidates}
        if len(distinct_candidates) <= 1:
            continue
        for card in distinct_candidates.values():
            card.mapping_state = ASOC_MATCH_CONFLICT
            card.mapping_detail = (
                f"Multiple ASOC products resolve to monitored product "
                f"{product_by_id[product_id].name}."
            )
            conflicted_product_ids.add(product_id)

    # Conflict is never an authoritative mapping. Keep the source URLs and
    # per-card match counts for diagnosis, but do not expose target links as
    # mapped evidence or leave a legacy one-to-one pointer behind.
    conflicted_asoc_ids = {
        card.asoc_id for card in cards if card.mapping_state == ASOC_MATCH_CONFLICT
    }
    if conflicted_asoc_ids:
        for card in cards:
            if card.asoc_id in conflicted_asoc_ids:
                conflicted_product_ids.update(target_ids_by_card.get(card.asoc_id, set()))
                target_ids_by_card.pop(card.asoc_id, None)
                card.monitor_product_id = None
        db.query(AsocProductRepositoryMapping).filter(
            AsocProductRepositoryMapping.asoc_id.in_(conflicted_asoc_ids)
        ).delete(synchronize_session="fetch")

    for card in cards:
        if (
            card.mapping_state in {ASOC_MATCH_MAPPED, ASOC_MATCH_PARTIAL}
            and target_ids_by_card.get(card.asoc_id)
            and not was_connected.get(card.asoc_id)
            and card.first_connected_at is None
        ):
            card.first_connected_at = card.synced_at or datetime.now(timezone.utc)

    for product in products:
        product.asoc_product_id = None
        product.asoc_product_slug = None
        product.asoc_sync_status = "missing_asoc"
    for product_id in conflicted_product_ids:
        if product_id in product_by_id:
            product_by_id[product_id].asoc_sync_status = ASOC_MATCH_CONFLICT
    for card in cards:
        for product_id in target_ids_by_card.get(card.asoc_id, set()):
            # A legacy Product row has room for one ASOC identity. Preserve it
            # only when this target is uniquely attributable.
            matches = candidate_cards.get(product_id, [])
            if len({candidate.asoc_id for candidate in matches}) > 1:
                product = product_by_id[product_id]
                product.asoc_sync_status = ASOC_MATCH_CONFLICT
                continue
            product = product_by_id[product_id]
            if card.mapping_state in {ASOC_MATCH_MAPPED, ASOC_MATCH_PARTIAL}:
                product.asoc_product_id = card.asoc_id
                product.asoc_product_slug = card.slug
                product.asoc_sync_status = card.mapping_state

    counts = {state: 0 for state in (
        ASOC_MATCH_MAPPED,
        ASOC_MATCH_PARTIAL,
        ASOC_MATCH_CONFLICT,
        ASOC_MATCH_MISSING_TARGET,
        ASOC_MATCH_NO_REPOSITORIES,
    )}
    for card in cards:
        counts[card.mapping_state] = counts.get(card.mapping_state, 0) + 1
    return counts


def _registry_version(cards: list[AsocProduct], incoming_ids: set[str]) -> str:
    """Bind confirmation to both current registry state and the exact ASOC IDs."""
    parts = []
    for card in sorted((row for row in cards if row.is_active), key=lambda row: row.asoc_id):
        synced = card.synced_at.isoformat() if card.synced_at else ""
        connected = card.first_connected_at.isoformat() if card.first_connected_at else ""
        parts.append("\x1f".join((card.asoc_id, synced, connected, card.mapping_state or "")))
    parts.append("-- incoming ASOC identities --")
    parts.extend(f"incoming:{asoc_id}" for asoc_id in sorted(incoming_ids))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _requires_removal_confirmation(active_count: int, removal_count: int, incoming_count: int) -> bool:
    if active_count == 0 or removal_count == 0:
        return False
    if incoming_count == 0:
        return True
    # Prompt at a quarter of the active catalog (rounded up), including small
    # registries where losing one of two cards is already a material change.
    return removal_count >= max(1, (active_count + 3) // 4)


def sync_asoc_products(
    db: Session,
    items: list[dict[str, Any]],
    repositories_by_product: dict[str, list[dict[str, Any]]] | None = None,
    *,
    confirm_mass_removal: bool = False,
    expected_active_count: int | None = None,
    expected_incoming_count: int | None = None,
    expected_registry_version: str | None = None,
    confirmed_by: str = "operator",
    unattended: bool = False,
) -> dict[str, int]:
    """Upsert ASOC product cards and map them from description GitLab links.

    ``repositories_by_product`` remains accepted for callers compiled against
    the previous signature, but it is intentionally ignored. Product mapping
    must not depend on ASOC repository membership or product slugs.
    """
    # Validate the complete response before touching the session. A duplicate
    # across pages means the upstream pagination window shifted; treating the
    # deduplicated rows as a complete snapshot could delete valid stored cards.
    if not isinstance(items, list):
        raise RuntimeError("ASOC products snapshot has an invalid schema")
    normalized_items: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("ASOC products snapshot contains an invalid product entry")
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise RuntimeError("ASOC products snapshot contains an invalid product id")
        asoc_id = str(raw_id).strip()
        name = item.get("name")
        if not asoc_id or not isinstance(name, str) or not name.strip():
            raise RuntimeError("ASOC products snapshot contains an invalid product entry")
        for field in ("slug", "description"):
            if item.get(field) is not None and not isinstance(item.get(field), str):
                raise RuntimeError("ASOC products snapshot contains an invalid product entry")
        repositories_count = item.get("repositoriesCount", 0)
        if repositories_count is None:
            repositories_count = 0
        if (
            isinstance(repositories_count, bool)
            or not isinstance(repositories_count, (int, str))
            or not str(repositories_count).isdigit()
        ):
            raise RuntimeError("ASOC products snapshot contains an invalid product entry")
        if asoc_id in seen:
            raise RuntimeError(
                "ASOC products response contains duplicate product id "
                f"{asoc_id}; refusing synchronization of an unstable snapshot"
            )
        seen.add(asoc_id)
        normalized_items.append((asoc_id, item))

    existing_rows = db.query(AsocProduct).all()
    existing = {row.asoc_id: row for row in existing_rows}
    active_count = sum(row.is_active for row in existing_rows)
    removal_count = sum(
        row.is_active and row.asoc_id not in seen for row in existing_rows
    )
    registry_version = _registry_version(existing_rows, seen)
    # An unattended run never archives: ASOC products are not normally
    # removed, so any removal waits for an operator instead of letting a
    # faulty upstream shrink the registry run by run.
    needs_confirmation = _requires_removal_confirmation(
        active_count, removal_count, len(seen)
    ) or (unattended and removal_count > 0)
    if needs_confirmation and (
        not confirm_mass_removal
        or expected_active_count != active_count
        or expected_incoming_count != len(seen)
        or expected_registry_version != registry_version
    ):
        raise AsocSyncConfirmationRequiredError(
            active_count=active_count,
            incoming_count=len(seen),
            removal_count=removal_count,
            registry_version=registry_version,
            stale_confirmation=confirm_mass_removal,
        )

    enable_asoc_lifecycle_writes(db)
    # Scrub legacy and tombstone descriptions as well as the current snapshot.
    # This makes a successful sync a cleanup boundary even if an older release
    # had stored raw ASOC text.
    for row in existing_rows:
        row.description = sanitize_description(row.description)
    synced_rows: list[AsocProduct] = []
    created = updated = 0
    synced_at = datetime.now(timezone.utc)
    for asoc_id, item in normalized_items:
        name = str(item.get("name") or "").strip()
        bi_id, title = parse_bi_id(name)
        row = existing.get(asoc_id)
        if row is None:
            row = AsocProduct(asoc_id=asoc_id)
            db.add(row)
            created += 1
        else:
            updated += 1
        synced_rows.append(row)
        row.name = name
        row.slug = (item.get("slug") or None)
        row.bi_id = bi_id
        row.title = title
        row.description = sanitize_description(item.get("description"))
        row.repositories_count = int(item.get("repositoriesCount", 0) or 0)
        # ``created`` is the quarterly reporting key: a snapshot that omits or
        # garbles it must not move an already-dated card out of its quarter.
        incoming_created = _parse_created(item.get("created"))
        if incoming_created is not None or plausible_created(row.asoc_created) is None:
            row.asoc_created = incoming_created
        row.synced_at = synced_at
        row.is_active = True
        row.removed_at = None
        if row.mapping_state is None:
            row.mapping_state = ASOC_MATCH_NOT_SYNCED
        if row.matched_repositories_count is None:
            row.matched_repositories_count = 0
    # Retain missing cards as tombstones: their first-connected timestamps and
    # last verified URL mappings are needed for quarter-over-quarter reports.
    removed = 0
    for asoc_id, row in existing.items():
        if asoc_id not in seen and row.is_active:
            row.is_active = False
            row.removed_at = synced_at
            row.mapping_state = ASOC_MATCH_REMOVED
            row.mapping_detail = "ASOC product card is no longer present in the latest complete snapshot."
            row.synced_at = synced_at
            removed += 1
    db.flush()
    mapping_counts = _apply_repository_mappings(db, synced_rows)
    if needs_confirmation or removed:
        # Unconfirmed rows keep a lasting trail of the smaller removals applied
        # automatically; the latest-attempt state is overwritten every run.
        db.add(
            AsocProductSyncEvent(
                actor=(confirmed_by or "operator")[:120],
                previous_active_count=active_count,
                incoming_count=len(seen),
                removed_count=removed,
                registry_version=registry_version,
                confirmed=needs_confirmation,
            )
        )
    db.commit()
    return {
        "total": len(seen),
        "created": created,
        "updated": updated,
        "removed": removed,
        "confirmation_used": int(needs_confirmation),
        **mapping_counts,
    }


def fetch_and_sync(
    db: Session,
    *,
    confirm_mass_removal: bool = False,
    expected_active_count: int | None = None,
    expected_incoming_count: int | None = None,
    expected_registry_version: str | None = None,
    confirmed_by: str = "operator",
    unattended: bool = False,
) -> dict[str, int]:
    client = AsocClient()
    if not client.enabled():
        raise RuntimeError("ASOC is not connected in Integrations")
    if not _ASOC_SYNC_LOCK.acquire(blocking=False):
        raise AsocSyncInProgressError("ASOC synchronization is already in progress")
    try:
        if db.get_bind().dialect.name == "postgresql":
            locked = db.execute(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": ASOC_SYNC_ADVISORY_LOCK_ID},
            ).scalar()
            if not locked:
                db.rollback()
                raise AsocSyncInProgressError(
                    "ASOC synchronization is already in progress"
                )
        items = client.list_products()
        return sync_asoc_products(
            db,
            items,
            confirm_mass_removal=confirm_mass_removal,
            expected_active_count=expected_active_count,
            expected_incoming_count=expected_incoming_count,
            expected_registry_version=expected_registry_version,
            confirmed_by=confirmed_by,
            unattended=unattended,
        )
    finally:
        try:
            if db.in_transaction():
                db.rollback()
        finally:
            _ASOC_SYNC_LOCK.release()


def asoc_sync_interval_minutes() -> int:
    """Automatic ASOC card refresh cadence for the pipeline worker (0 = off)."""
    try:
        return max(int(os.getenv("ASOC_PRODUCT_SYNC_INTERVAL_MINUTES", "60")), 0)
    except ValueError:
        return 60


def _stored_attempt_is_newer(stored: Optional[str], at: str) -> bool:
    try:
        previous = json.loads(stored or "")
        return datetime.fromisoformat(previous["at"]) > datetime.fromisoformat(at)
    except (ValueError, TypeError, KeyError):
        return False


def record_sync_outcome(
    db: Session,
    *,
    trigger: str,
    status: str,
    message: str,
    result: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Persist the latest ASOC card sync attempt for the Products view."""
    outcome = {
        "trigger": trigger,
        "status": status,
        "message": message[:500],
        "at": (at or datetime.now(timezone.utc)).isoformat(),
        "result": {key: int(value) for key, value in (result or {}).items() if isinstance(value, int)},
    }
    value = json.dumps(outcome)
    def locked_row():
        # Refresh any identity-map entry after obtaining the database row lock.
        return (db.query(AppState).filter(AppState.key == ASOC_SYNC_STATE_KEY)
                .populate_existing().with_for_update().first())

    row = locked_row()
    if row is None:
        db.add(AppState(key=ASOC_SYNC_STATE_KEY, value=value))
        try:
            db.commit()
            return outcome
        except IntegrityError:
            db.rollback()
            row = locked_row()
            if row is None:
                raise RuntimeError('ASOC outcome row disappeared after concurrent insertion') from None
    if _stored_attempt_is_newer(row.value, outcome["at"]):
        # Recheck only while holding the lock; a slower old attempt cannot win.
        db.commit()
        return outcome
    row.value = value
    db.commit()
    return outcome


def read_sync_outcome(db: Session) -> dict[str, Any] | None:
    row = db.query(AppState).filter(AppState.key == ASOC_SYNC_STATE_KEY).first()
    if row is None or not row.value:
        return None
    try:
        value = json.loads(row.value)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def sync_result_message(result: dict[str, Any]) -> str:
    linked = int(result.get("mapped", 0)) + int(result.get("partial", 0))
    review = (
        int(result.get("conflict", 0))
        + int(result.get("missing_target", 0))
        + int(result.get("no_repositories", 0))
    )
    return (
        f"{int(result.get('total', 0))} ASOC products: {linked} synced with ScanOps, "
        f"{review} need attention, {int(result.get('removed', 0))} removed."
    )


def run_scheduled_sync(db: Session) -> dict[str, Any] | None:
    """Refresh ASOC cards without operator input.

    New and changed cards are applied automatically so a product appears in
    ScanOps soon after it is created in ASOC. A snapshot that would archive
    any card is never applied here: the attempt is recorded and waits for an
    operator's Synchronize ASOC.
    """
    if not AsocClient().enabled():
        return None
    started_at = datetime.now(timezone.utc)
    try:
        result = fetch_and_sync(db, confirmed_by="scheduler", unattended=True)
    except AsocSyncInProgressError:
        return None
    except AsocSyncConfirmationRequiredError as exc:
        db.rollback()
        return record_sync_outcome(
            db,
            trigger="scheduler",
            at=started_at,
            status="needs_confirmation",
            message=(
                f"ASOC returned {exc.incoming_count} products while {exc.active_count} are active; "
                f"{exc.removal_count} would be removed. Automatic sync never removes products; "
                "run Synchronize ASOC to review and apply."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the worker must keep running
        db.rollback()
        correlation_id = uuid.uuid4().hex
        logger.warning(
            "asoc_product_auto_sync_failed correlation_id=%s error_type=%s",
            correlation_id,
            type(exc).__name__,
        )
        # Like the operator path, never persist upstream error text: it is
        # served back to every Products viewer.
        return record_sync_outcome(
            db,
            trigger="scheduler",
            at=started_at,
            status="failed",
            message=f"ASOC synchronization failed (reference {correlation_id}).",
        )
    return record_sync_outcome(
        db,
        trigger="scheduler",
        at=started_at,
        status="ok",
        message=sync_result_message(result),
        result=result,
    )


def asoc_sync_metadata(
    db: Session,
    *,
    configured: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a safe, operator-facing ASOC card synchronization snapshot."""
    last_synced = db.query(func.max(AsocProduct.synced_at)).scalar()
    active_query = db.query(AsocProduct).filter(AsocProduct.is_active.is_(True))
    total_cards = int(active_query.count())
    historical_cards = int(db.query(func.count(AsocProduct.id)).filter(
        AsocProduct.is_active.is_(False)
    ).scalar() or 0)
    mapping_counts = {
        state: int(count)
        for state, count in db.query(
            AsocProduct.mapping_state,
            func.count(AsocProduct.id),
        ).filter(AsocProduct.is_active.is_(True)).group_by(AsocProduct.mapping_state)
    }
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if last_synced is not None and last_synced.tzinfo is None:
        last_synced = last_synced.replace(tzinfo=timezone.utc)

    age_seconds = (
        max(int((current - last_synced).total_seconds()), 0)
        if last_synced is not None
        else None
    )
    if not configured:
        state = "not_configured"
    elif last_synced is None:
        state = "not_synced"
    elif age_seconds is not None and age_seconds > int(ASOC_SYNC_STALE_AFTER.total_seconds()):
        state = "stale"
    else:
        state = "synced"
    return {
        "state": state,
        "configured": configured,
        "last_synced": last_synced,
        "age_seconds": age_seconds,
        "total_cards": total_cards,
        "historical_cards": historical_cards,
        "mapping_counts": mapping_counts,
    }


def mapping_state(bi_id: str | None, monitor_product: Product | None) -> str:
    """Return the operator-facing ASOC-to-Target mapping state."""
    if not bi_id:
        return MAPPING_MISSING_BI_ID
    if monitor_product is None:
        return MAPPING_MISSING_TARGET
    return MAPPING_MAPPED


def report_mapping_state(
    card: AsocProduct,
    monitor_product: Product | None,
) -> str:
    """Expose ambiguous and partial evidence without labeling it as mapped."""
    if card.mapping_state == ASOC_MATCH_CONFLICT:
        return ASOC_MATCH_CONFLICT
    if not card.is_active:
        return ASOC_MATCH_REMOVED
    if card.mapping_state == ASOC_MATCH_PARTIAL:
        return ASOC_MATCH_PARTIAL
    return mapping_state(card.bi_id, monitor_product)


def report_rows(db: Session) -> list[dict[str, Any]]:
    """Every ASOC card with its monitoring linkage (operator product pages)."""
    cards = db.query(AsocProduct).order_by(AsocProduct.bi_id.isnot(None).desc(), AsocProduct.title.asc()).all()

    # Monitoring linkage in bulk queries (no per-card N+1).
    products = db.query(Product).all()
    products_by_id = {product.id: product for product in products}
    repo_counts = dict(
        db.query(Repository.product_id, func.count(Repository.id)).group_by(Repository.product_id).all()
    )
    target_ids_by_asoc: dict[str, set[int]] = defaultdict(set)
    mappings = db.query(AsocProductRepositoryMapping).all()
    for mapping in mappings:
        target_ids_by_asoc[mapping.asoc_id].add(mapping.product_id)

    rows: list[dict[str, Any]] = []
    for card in cards:
        target_ids = target_ids_by_asoc.get(card.asoc_id, set())
        if not target_ids and card.monitor_product_id is not None:
            target_ids = {card.monitor_product_id}
        monitor_products = [
            products_by_id[product_id]
            for product_id in sorted(target_ids)
            if product_id in products_by_id
        ]
        monitor = monitor_products[0] if monitor_products else None
        matched_by = "description_url" if monitor else None
        rows.append(
            {
                "asoc_id": card.asoc_id,
                "bi_id": card.bi_id,
                "title": card.title or card.name,
                "asoc_name": card.name,
                "slug": card.slug,
                "asoc_created": plausible_created(card.asoc_created),
                "first_connected_at": card.first_connected_at,
                "connected_before_tracking": bool(card.connected_before_tracking),
                "scope": product_scope(card.description),
                "description_links": safe_description_links(card.description),
                "monitor_product": monitor,
                "monitor_products": monitor_products,
                "monitor_repos": repo_counts.get(monitor.id, 0) if monitor else 0,
                "monitor_repositories": sum(
                    repo_counts.get(product.id, 0) for product in monitor_products
                ),
                "monitor_repository_counts": {
                    str(product.id): repo_counts.get(product.id, 0)
                    for product in monitor_products
                },
                "matched_by": matched_by,
                "repository_mapping_state": card.mapping_state,
                "asoc_repositories": card.repositories_count,
                "matched_repositories": card.matched_repositories_count,
                "mapping_detail": card.mapping_detail,
                "is_active": card.is_active,
                "removed_at": card.removed_at,
                "mapping_state": report_mapping_state(card, monitor),
            }
        )
    return rows


def asoc_rows_by_product(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Group mapped report rows for product-centric views."""
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("is_active", True):
            continue
        monitor_products = row.get("monitor_products") or []
        if not monitor_products and row.get("monitor_product") is not None:
            monitor_products = [row["monitor_product"]]
        for monitor in monitor_products:
            grouped[monitor.id].append(row)
    return dict(grouped)
