from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.types import TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
import enum
from .database import Base


class JobStatus(enum.IntEnum):
    SUCCESS = 1
    FAILED = 2
    RUNNING = 3
    CANCELLED = 4
    SKIPPED = 5

    @property
    def label(self) -> str:
        return _JOB_STATUS_LABELS[self]


_JOB_STATUS_LABELS = {
    JobStatus.SUCCESS: "success",
    JobStatus.FAILED: "failed",
    JobStatus.RUNNING: "running",
    JobStatus.CANCELLED: "cancelled",
    JobStatus.SKIPPED: "skipped",
}

_JOB_STATUS_BY_NAME = {value: key for key, value in _JOB_STATUS_LABELS.items()}


class JobStatusType(TypeDecorator):
    """Persist JobStatus enums as SMALLINT while exposing IntEnum to SQLAlchemy code."""

    impl = SmallInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, JobStatus):
            return int(value)
        if isinstance(value, str):
            return int(_JOB_STATUS_BY_NAME.get(value.lower(), JobStatus.FAILED))
        if isinstance(value, int):
            return value
        raise ValueError(f"Unsupported job status value: {value}")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return JobStatus(value)


class JobReport(Base):
    __tablename__ = "job_reports"

    id = Column(Integer, primary_key=True)
    repository_id = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    job_name = Column(String(100), nullable=False)
    job_status = Column(JobStatusType(), nullable=False)
    pipeline_url = Column(String(255), nullable=True)
    ref = Column(String(120), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_job_reports_repo_lookup", "repository_id", "job_name", "ref", "finished_at"),
        Index("ix_job_reports_status", "job_status", "finished_at"),
    )

    # Relationships
    repository = relationship("Repository", back_populates="job_reports")
    detail = relationship(
        "JobReportDetail",
        back_populates="job_report",
        cascade="all, delete-orphan",
        uselist=False,
    )


class JobReportDetail(Base):
    __tablename__ = "job_report_details"

    job_report_id = Column(Integer, ForeignKey("job_reports.id", ondelete="CASCADE"), primary_key=True)
    raw_payload = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    job_report = relationship("JobReport", back_populates="detail")
