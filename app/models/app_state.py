from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.sql import func

from .database import Base


class AppState(Base):
    """Small key-value store for worker cursors and last-run markers."""

    __tablename__ = "app_state"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AsocEnrichedIssue(Base):
    """Tracks which Vampy issues already received a KEV/EPSS annotation.

    Composite PK (issue_id, kind): an issue annotated for EPSS can later get a
    separate KEV annotation when the CVE lands in the KEV catalogue.
    """

    __tablename__ = "asoc_enriched_issues"

    issue_id = Column(String(64), primary_key=True)
    kind = Column(String(16), primary_key=True)  # 'kev' | 'epss'
    cve = Column(String(32), nullable=False)
    applied_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
