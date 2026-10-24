from urllib.parse import urlparse

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class IacScanRun(Base):
    __tablename__ = "iac_scan_runs"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    trigger = Column(String(32), nullable=False, default="manual")
    target_url = Column(String, nullable=False)
    gitlab_base_url = Column(String, nullable=False)
    scope_path = Column(String, nullable=False, index=True)
    # Set for a single-repository rescan: the worker scans only this GitLab
    # project instead of listing the whole group under scope_path.
    single_project_id = Column(Integer, nullable=True)
    scanner_name = Column(String(64), nullable=False, default="checkov")
    total_repositories = Column(Integer, default=0, nullable=False)
    candidate_repositories = Column(Integer, default=0, nullable=False)
    scanned_repositories = Column(Integer, default=0, nullable=False)
    uploaded_repositories = Column(Integer, default=0, nullable=False)
    findings_count = Column(Integer, default=0, nullable=False)
    summary_json = Column(Text, nullable=True)
    error_text = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    repositories = relationship("IacScanRepository", back_populates="run", cascade="all, delete-orphan")
    findings = relationship("IacScanFinding", back_populates="run", cascade="all, delete-orphan")

    @property
    def is_rescan(self) -> bool:
        return self.trigger == "rescan" or self.single_project_id is not None

    @property
    def target_label(self) -> str:
        """What this run scanned, for the UI: the group scope for group runs,
        the repository path for single-repo rescans (scope_path there is the
        parent run's group and reads like a full group scan)."""
        if self.is_rescan and self.target_url:
            path = urlparse(self.target_url).path.strip("/")
            if path:
                return path
        return self.scope_path


class IacScanRepository(Base):
    __tablename__ = "iac_scan_repositories"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("iac_scan_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    repository_id = Column(Integer, ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    gitlab_project_id = Column(Integer, nullable=False, index=True)
    project_path = Column(String, nullable=False, index=True)
    project_url = Column(String, nullable=True)
    default_branch = Column(String, nullable=True)
    clone_url = Column(String, nullable=True)
    has_iac = Column(Boolean, nullable=False, default=False, index=True)
    iac_markers = Column(Text, nullable=True)
    finder_status = Column(String(32), nullable=False, default="queued")
    scan_status = Column(String(32), nullable=False, default="queued", index=True)
    asoc_upload_status = Column(String(32), nullable=False, default="pending", index=True)
    asoc_product_slug = Column(String, nullable=True)
    asoc_product_id = Column(String, nullable=True)
    # ASOC repository the SARIF is pushed into (slug = <instance-prefix>/<project_path>).
    asoc_repository_slug = Column(String, nullable=True)
    asoc_repository_id = Column(String, nullable=True)
    sarif_path = Column(String, nullable=True)
    critical_count = Column(Integer, default=0, nullable=False)
    high_count = Column(Integer, default=0, nullable=False)
    medium_count = Column(Integer, default=0, nullable=False)
    low_count = Column(Integer, default=0, nullable=False)
    info_count = Column(Integer, default=0, nullable=False)
    findings_count = Column(Integer, default=0, nullable=False)
    error_text = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    run = relationship("IacScanRun", back_populates="repositories")
    product = relationship("Product")
    repository = relationship("Repository")
    findings = relationship("IacScanFinding", back_populates="scan_repository", cascade="all, delete-orphan")


class IacScanFinding(Base):
    __tablename__ = "iac_scan_findings"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("iac_scan_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    scan_repository_id = Column(Integer, ForeignKey("iac_scan_repositories.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    repository_id = Column(Integer, ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    project_path = Column(String, nullable=False, index=True)
    project_url = Column(String, nullable=True)
    severity = Column(String(16), nullable=False, index=True)
    rule_id = Column(String(160), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    recommendation = Column(Text, nullable=True)
    file_path = Column(String, nullable=True)
    line = Column(Integer, nullable=True)
    evidence = Column(Text, nullable=True)
    scanner_name = Column(String(64), nullable=False, default="checkov")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    run = relationship("IacScanRun", back_populates="findings")
    scan_repository = relationship("IacScanRepository", back_populates="findings")
    product = relationship("Product")
    repository = relationship("Repository")
