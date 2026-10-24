from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base

# ASOC ScanPipelineStatus values that mean the scan is done; we stop polling.
PTAI_TERMINAL_STATUSES = frozenset({"ERROR", "SUCCESS", "PARTIAL_SUCCESS"})
# Statuses we alert on as a failed scan.
PTAI_FAILED_STATUSES = frozenset({"ERROR", "PARTIAL_SUCCESS"})

# PT AI engine Stage enum: terminal stages stop engine polling.
PTAI_ENGINE_TERMINAL_STAGES = frozenset({"Done", "Failed", "Aborted"})
PTAI_ENGINE_FAILED_STAGES = frozenset({"Failed", "Aborted"})


class PtaiScan(Base):
    """One async PT AI scan triggered by a GitLab `ptai_scan` job.

    Identifiers are parsed from the job trace; ``asoc_status`` mirrors the ASOC
    ScanPipelineStatus enum, with two local sentinels: ``PENDING`` before the
    first poll and ``TRIGGER_FAILED`` when the trace carried no ASOC ids.
    """

    __tablename__ = "ptai_scans"

    id = Column(Integer, primary_key=True)
    gitlab_project_id = Column(Integer, nullable=False)
    gitlab_pipeline_id = Column(Integer, nullable=False)
    gitlab_job_id = Column(Integer, nullable=False, unique=True)
    repository_id = Column(Integer, ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    asoc_scan_id = Column(String, nullable=True)
    asoc_repository_id = Column(String, nullable=True)
    asoc_task_id = Column(String, nullable=True)
    asoc_pipeline_id = Column(String, nullable=True)
    repo_slug = Column(String, nullable=True)
    branch = Column(String, nullable=True)
    asoc_status = Column(String(32), nullable=False, default="PENDING")
    trigger_failed = Column(Boolean, nullable=False, default=False)
    job_web_url = Column(String, nullable=True)
    error_text = Column(Text, nullable=True)
    last_polled_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    last_alert_at = Column(DateTime(timezone=True), nullable=True)
    # Direct PT AI engine status (resolved by project name + branch).
    ptai_project_id = Column(String, nullable=True)
    ptai_branch_id = Column(String, nullable=True)
    ptai_scan_result_id = Column(String, nullable=True)
    ptai_stage = Column(String(32), nullable=True)
    ptai_progress = Column(Integer, nullable=True)
    ptai_has_errors = Column(Boolean, nullable=True)
    ptai_error = Column(Text, nullable=True)
    ptai_polled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")
    product = relationship("Product")

    @property
    def is_terminal(self) -> bool:
        return self.trigger_failed or self.asoc_status in PTAI_TERMINAL_STATUSES
