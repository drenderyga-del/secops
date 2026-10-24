"""Expected-versus-configured coverage for the shared control vocabulary."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from .models.security_control import RepositoryControlBinding, SecurityControl


BINDING_STATUSES = frozenset({"configured", "missing", "unknown", "excluded"})


def coverage_by_repository(db: Session, repository_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """Return coverage without assigning semantics to an execution or scanner.

    ``excluded`` bindings remain visible in details but are not expected for
    coverage purposes. ``unknown`` is intentionally counted as not configured.
    """
    ids = list({int(repository_id) for repository_id in repository_ids})
    result = defaultdict(lambda: {"expected": 0, "configured": 0, "missing": 0, "excluded": 0})
    if not ids:
        return {}

    rows = (
        db.query(
            RepositoryControlBinding.repository_id,
            func.coalesce(
                func.sum(case((RepositoryControlBinding.status != "excluded", 1), else_=0)),
                0,
            ).label("expected"),
            func.coalesce(
                func.sum(case((RepositoryControlBinding.status == "configured", 1), else_=0)),
                0,
            ).label("configured"),
            func.coalesce(
                func.sum(case((RepositoryControlBinding.status == "excluded", 1), else_=0)),
                0,
            ).label("excluded"),
        )
        .join(SecurityControl, RepositoryControlBinding.security_control_id == SecurityControl.id)
        .filter(RepositoryControlBinding.repository_id.in_(ids))
        .filter(SecurityControl.is_active.is_(True))
        .group_by(RepositoryControlBinding.repository_id)
        .all()
    )
    for row in rows:
        expected = int(row.expected or 0)
        configured = int(row.configured or 0)
        result[row.repository_id] = {
            "expected": expected,
            "configured": configured,
            "missing": max(expected - configured, 0),
            "excluded": int(row.excluded or 0),
        }
    return dict(result)
