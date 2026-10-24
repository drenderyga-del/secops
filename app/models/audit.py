import enum
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    Boolean,
    Enum,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class AuditCoverageStatus(str, enum.Enum):
    COVERED = "covered"
    PIPELINE_NO_SEC = "pipeline_no_sec"
    NO_PIPELINES = "no_pipelines"
    INACCESSIBLE = "inaccessible"
    EXCLUDED = "excluded"


class AuditRepository(Base):
    __tablename__ = "audit_repositories"
    __table_args__ = (
        UniqueConstraint("instance_host", "project_id", name="uq_audit_repo_instance_project"),
    )

    id = Column(Integer, primary_key=True)
    instance_host = Column(String(255), nullable=False, index=True)
    project_id = Column(Integer, nullable=False)
    path_with_namespace = Column(String(255), nullable=False, index=True)
    web_url = Column(String(255), nullable=True)
    default_branch = Column(String(120), nullable=True)
    archived = Column(Boolean, nullable=False, default=False)
    empty_repo = Column(Boolean, nullable=False, default=False)
    excluded = Column(Boolean, nullable=False, default=False)
    exclude_reason = Column(String(255), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    snapshots = relationship("AuditSnapshot", back_populates="repository", cascade="all, delete-orphan")


class AuditSnapshot(Base):
    __tablename__ = "audit_snapshots"
    __table_args__ = (
        Index("ix_audit_snapshots_repo_scanned", "audit_repository_id", "scanned_at"),
        Index("ix_audit_snapshots_status_scanned", "status", "scanned_at"),
    )

    id = Column(Integer, primary_key=True)
    audit_repository_id = Column(Integer, ForeignKey("audit_repositories.id", ondelete="CASCADE"), nullable=False)
    scanned_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    window_days = Column(Integer, nullable=False, default=30)
    status = Column(Enum(AuditCoverageStatus, native_enum=False, length=24), nullable=False)
    reason = Column(Text, nullable=True)
    pipeline_count = Column(Integer, nullable=False, default=0)
    security_job_count = Column(Integer, nullable=False, default=0)
    last_pipeline_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)

    repository = relationship("AuditRepository", back_populates="snapshots")


class AuditScanRun(Base):
    __tablename__ = "audit_scan_runs"
    __table_args__ = (
        Index("ix_audit_scan_runs_status_created", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    requested_product_ids = Column(Text, nullable=True)
    selected_products = Column(Integer, nullable=False, default=0)
    total_repositories = Column(Integer, nullable=False, default=0)
    processed_repositories = Column(Integer, nullable=False, default=0)
    message = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
